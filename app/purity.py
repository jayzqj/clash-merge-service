"""Mandatory current-round connectivity; scoring is independent and optional.

TCP backend nodes use a front verified during this round. Output pins that
exact front, so client selection cannot silently change the tested chain.
"""
from __future__ import annotations

import logging
import re
import os
import shutil
import subprocess
import time
import secrets
import socket
from pathlib import Path
from urllib.parse import quote
from app.dialer import is_backend, is_udp
from app.purity_history import apply_history, filter_by_purity
from app.names import CC_TO_CN

import httpx
import yaml

from app.merge import dedup_key, expected_ip
from app.processes import start_process, stop_process, STOPPING

log = logging.getLogger(__name__)

SELECTOR_GROUP = "MERGE-CHECK"
UDP_TYPES = ("hysteria2", "tuic", "wireguard")
INFO_TIMEOUT = 30

# Both sequential and batch mode make at most two connectivity attempts.
DELAY_URL = "https://cp.cloudflare.com/generate_204"
DELAY_ROUNDS = 2
MAX_FRONT_CANDIDATES = 8


def diversify_fronts(candidates, results):
    """Keep low-delay order while spreading choices over regions and endpoints."""
    selected, regions, endpoints = [], set(), set()
    for p in candidates:
        rec = results.get(dedup_key(p), {})
        flag = re.search(r"[\U0001f1e6-\U0001f1ff]{2}", p["name"])
        region = rec.get("country") or next((cc for cc, cn in CC_TO_CN.items() if cn in p["name"]), None)
        region = region or (flag.group() if flag else str(p.get("server")))
        endpoint = (p.get("server"), p.get("type"))
        if region in regions or endpoint in endpoints:
            continue
        selected.append(p); regions.add(region); endpoints.add(endpoint)
        if len(selected) == MAX_FRONT_CANDIDATES:
            return selected
    for p in candidates:
        if p not in selected:
            selected.append(p)
            if len(selected) == MAX_FRONT_CANDIDATES:
                break
    return selected


def ippure_url(ip: str | None) -> str:
    return f"https://ippure.com/?ip={ip}" if ip else "https://ippure.com/"


def _worker_workdir(base: Path, worker_id: int) -> Path:
    """Per-worker mihomo home dir. Worker 0 keeps the base dir itself
    (unchanged layout, geodata already there); other workers get a
    subdirectory with symlinks to the base dir's geodata files, so
    two mihomo processes never share one home dir."""
    if worker_id == 0:
        return base
    d = base / f"worker-{worker_id}"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("geosite.dat", "geoip.dat", "geoip.metadb", "Country.mmdb"):
        src = base / name
        dst = d / name
        if src.exists() and not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                shutil.copy(src, dst)
    return d


def _shard(proxies: list[dict], workers: int) -> list[list[dict]]:
    """Round-robin split: every worker gets a mix of front and
    backend nodes (a contiguous split would put all 172 静态 nodes
    in the last shard(s), leaving other workers without a front)."""
    shards: list[list[dict]] = [[] for _ in range(workers)]
    for i, p in enumerate(proxies):
        shards[i % workers].append(p)
    return [s for s in shards if s]


def build_mihomo_config(proxies: list[dict], front_name: str | None,
                         mixed_port: int, controller_port: int,
                         extra_proxies: list[dict] | None = None) -> dict:
    """``extra_proxies`` (e.g. the configured check front) are added
    to the config so ``dialer-proxy`` can reference them, but are
    NOT put in the selector group and are never checked."""
    cfg_proxies = []
    for p in proxies:
        node = dict(p)
        # Never inherit a dialer-proxy from the source: by default
        # every node (backend included) is checked direct.
        node.pop("dialer-proxy", None)
        # Only with an explicitly enabled check front do backend TCP
        # nodes ride it; UDP nodes cannot carry dialer-proxy.
        if front_name and "静态" in str(node.get("name", "")):
            if str(node.get("type", "")).lower() not in UDP_TYPES:
                node["dialer-proxy"] = front_name
        cfg_proxies.append(node)
    for p in (extra_proxies or []):
        cfg_proxies.append(dict(p))
    # NOTE: the rule MUST send traffic through the selector group.
    # An earlier version used ["MATCH,DIRECT"] here, which silently
    # routed every check straight out of the VPS: the PUT that
    # selects a node then had no effect at all, every node measured
    # the VPS egress IP (fraudScore 49 for all 323 nodes in the
    # 2026-11-12 22:20 run), and most exit IPs "mismatched". With
    # MATCH,<selector-group> the info request goes through the node
    # currently selected in MERGE-CHECK (backend TCP nodes ride the
    # front via their dialer-proxy, as set above).
    return {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{controller_port}",
        "allow-lan": False,
        # The Bright Data gateway is only reachable over IPv4 from
        # the VPS; IPv6 resolution would make direct checks flaky.
        "ipv6": False,
        "log-level": "warning",
        "proxies": cfg_proxies,
        "proxy-groups": [
            {"name": SELECTOR_GROUP, "type": "select",
             "proxies": [str(p["name"]) for p in proxies]},
        ],
        "rules": [f"MATCH,{SELECTOR_GROUP}"],
    }


class MihomoChecker:
    def __init__(self, config, worker_id: int = 0):
        self.cfg = config
        self.worker_id = worker_id
        self.proc: subprocess.Popen | None = None
        base_workdir = Path(config.data.get("mihomo_workdir",
                                            "/opt/clash-merge-service/mihomo"))
        self.workdir = _worker_workdir(base_workdir, worker_id)
        # Each worker owns its ports: worker i uses base+i, so N
        # mihomo instances never collide.
        self.mixed_port = int(config.data.get("mihomo_mixed_port", 7890)) + worker_id
        self.controller_port = int(config.data.get("mihomo_controller_port", 9090)) + worker_id
        self.info_url = config.data.get("purity_info_url",
                                        "https://my.123169.xyz/v1/info")
        self.controller_secret = secrets.token_urlsafe(24)
        self.front_internal_name = "__merge_front_" + secrets.token_hex(8)
        self.score_failures = 0
        self.summary = {}
        # Assigned by the backend stage after this round has verified the front.
        self.check_front: dict | None = None

    def _controller(self, path: str) -> str:
        return f"http://127.0.0.1:{self.controller_port}{path}"

    def start(self, proxies: list[dict]) -> bool:
        binary = shutil.which(str(self.cfg.data.get("mihomo_bin", "mihomo")))
        if not binary:
            log.warning("purity: mihomo binary not found (%s), skipping check",
                        self.cfg.data.get("mihomo_bin"))
            return False
        # Refuse occupied ports; never connect to an existing Clash instance.
        for port in (self.mixed_port, self.controller_port):
            with socket.socket() as probe:
                if os.name != "nt":
                    # A preceding test stage may leave TIME_WAIT connections.
                    # Match Go's listener reuse, while still rejecting a live listener.
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    log.warning("purity worker %d: port %d is occupied", self.worker_id, port)
                    return False
        if self.check_front:
            # Renamed internally: a check front sharing its name with
            # a subscription node would make mihomo refuse the config
            # (duplicate proxy name) and lose the whole round.
            front_name: str | None = self.front_internal_name
            extra = [dict(self.check_front, name=self.front_internal_name)]
            extra[0].pop("dialer-proxy", None)
            log.info("purity worker %d: backend checks ride configured "
                     "front %s", self.worker_id,
                     self.check_front.get("name", ""))
        else:
            front_name = None
            extra = []
        cfg = build_mihomo_config(proxies, front_name, self.mixed_port,
                                  self.controller_port, extra_proxies=extra)
        cfg["secret"] = self.controller_secret
        self.workdir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.workdir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True,
                                           sort_keys=False), encoding="utf-8")
        # The child keeps its own copy of the fd; close ours right
        # away so every round does not leak one file handle.
        with open(self.workdir / "mihomo.log", "a", encoding="utf-8") as logfile:
            self.proc = start_process(
                [binary, "-f", str(cfg_path), "-d", str(self.workdir)],
                stdout=logfile, stderr=subprocess.STDOUT)
        for _ in range(30):
            if self.proc.poll() is not None:
                log.warning("purity worker %d: mihomo exited early (code %s), "
                            "see %s", self.worker_id, self.proc.returncode,
                            self.workdir / "mihomo.log")
                self.stop()
                return False
            try:
                r = httpx.get(self._controller("/version"), timeout=3,
                              headers={"Authorization": f"Bearer {self.controller_secret}"},
                              trust_env=False)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(1)
        log.warning("purity: mihomo controller did not come up")
        self.stop()
        return False

    def stop(self) -> None:
        if self.proc is not None:
            stop_process(self.proc)
            self.proc = None

    def _select(self, name: str) -> None:
        r = httpx.put(self._controller(f"/proxies/{SELECTOR_GROUP}"),
                      headers={"Authorization": f"Bearer {self.controller_secret}"},
                      json={"name": name}, timeout=10, trust_env=False)
        r.raise_for_status()

    def _info_once(self, timeout: float = INFO_TIMEOUT) -> dict:
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        with httpx.Client(proxy=proxy, timeout=timeout,
                          trust_env=False) as client:
            r = client.get(self.info_url,
                           headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()


    def check_connection(self, proxy):
        """Actual HTTP request through this node/chain, independent of scoring."""
        url = str(self.cfg.data.get("connectivity_url") or DELAY_URL)
        timeout = int(self.cfg.data.get("connectivity_timeout_ms", 10000))
        for _ in range(DELAY_ROUNDS):
            if STOPPING.is_set():
                break
            try:
                r = httpx.get(self._controller("/proxies/" + quote(str(proxy["name"]), safe="") + "/delay"),
                              headers={"Authorization": f"Bearer {self.controller_secret}"},
                              params={"url": url, "timeout": timeout, "expected": "200-299"}, timeout=timeout / 1000 + 5, trust_env=False)
                r.raise_for_status()
                delay = r.json().get("delay")
                if type(delay) is int and delay > 0 and proxy["name"] in self.confirm_http_success(url, [proxy["name"]]):
                    return delay
            except (httpx.HTTPError, ValueError):
                pass
        return None

    def confirm_http_success(self, url, names):
        # Older Mihomo APIs can return a nonzero delay for an unexpected HTTP
        # status. The URL-specific health flag includes the expected-status check.
        r = httpx.get(self._controller("/proxies"),
                      headers={"Authorization": f"Bearer {self.controller_secret}"},
                      timeout=5, trust_env=False)
        r.raise_for_status()
        proxies = r.json().get("proxies", {})
        return {name for name in names
                if proxies.get(name, {}).get("extra", {}).get(url, {}).get("alive") is True}

    def connection_batch(self, proxies):
        if not self.cfg.data.get("purity_delay_screen", True):
            return {p["name"]: delay for p in proxies if (delay := self.check_connection(p)) is not None}
        url = str(self.cfg.data.get("connectivity_url") or DELAY_URL)
        timeout = int(self.cfg.data.get("connectivity_timeout_ms", 10000))
        passed = {}
        for _ in range(DELAY_ROUNDS):
            if STOPPING.is_set():
                break
            try:
                r = httpx.get(self._controller(f"/group/{SELECTOR_GROUP}/delay"),
                              headers={"Authorization": f"Bearer {self.controller_secret}"},
                              params={"url": url, "timeout": timeout, "expected": "200-299"}, timeout=timeout / 1000 + 20, trust_env=False)
                if r.status_code == 504:
                    continue  # controller explicitly reports no node passed
                r.raise_for_status()
                data = r.json()
                http_ok = self.confirm_http_success(url, [p["name"] for p in proxies])
                passed.update({p["name"]: data[p["name"]] for p in proxies
                               if p["name"] in http_ok and type(data.get(p["name"])) is int and data[p["name"]] > 0})
                if len(passed) == len(proxies):
                    break
            except (httpx.HTTPError, ValueError, TypeError):
                # Unsupported batch endpoint or transient controller error:
                # verify the remaining nodes individually, never count as passed.
                for p in proxies:
                    if p["name"] not in passed:
                        delay = self.check_connection(p)
                        if delay is not None:
                            passed[p["name"]] = delay
                break
        return passed

    def check_one(self, proxy, attempts=2, timeout=10):
        """Only the score lookup. A failure here never changes connected=True."""
        empty = {"measured": False, "score": None, "attempts": 0}
        if not self.cfg.data.get("purity_enabled", True):
            return {**empty, "score_error": "纯净度检测已关闭"}
        if self.score_failures >= 3:
            return {**empty, "score_error": "该检测链路连续三次评分失败，本分片暂停评分"}
        last = ""
        for attempt in range(attempts):
            if STOPPING.is_set():
                break
            try:
                self._select(str(proxy["name"]))
                data = self._info_once(timeout)
                score = data.get("fraudScore")
                if type(score) not in (int, float) or not 0 <= score <= 100:
                    raise ValueError("评分接口没有返回有效分数")
                self.score_failures = 0
                expected = expected_ip(proxy)
                return {"measured": True, "score": int(score), "exit_ip": data.get("ip"),
                        "country": data.get("countryCode", ""), "expected_ip": expected,
                        "match": data.get("ip") == expected if expected else None,
                        "ippure_url": ippure_url(data.get("ip")), "attempts": attempt + 1}
            except Exception as exc:
                last = type(exc).__name__
        self.score_failures += 1
        return {**empty, "attempts": attempts, "score_error": last}

    def _check_shard(self, shard, worker_id, front=None):
        checker = MihomoChecker(self.cfg, worker_id=worker_id)
        checker.check_front = front
        if not checker.start(shard):
            raise RuntimeError(f"检测内核 worker {worker_id} 启动失败，请检查端口和内核日志")
        results = {}
        try:
            passed = checker.connection_batch(shard)
            log.info("worker %d: connectivity passed %d/%d; front=%s", worker_id,
                     len(passed), len(shard), front.get("name") if front else "direct")
            for p in shard:
                if STOPPING.is_set():
                    raise RuntimeError("application is shutting down")
                if checker.proc.poll() is not None:
                    raise RuntimeError("检测内核在本轮中途退出")
                connected = p["name"] in passed
                rec = {"connected": connected, "delay_ms": passed.get(p["name"]),
                       "connectivity_attempts": DELAY_ROUNDS, "measured": False, "score": None, "attempts": 0}
                if front is not None:
                    rec["front_key"] = dedup_key(front)
                if connected:
                    rec.update(checker.check_one(p))
                else:
                    rec["connectivity_error"] = "两轮 HTTP 连通性测试均未通过"
                results[dedup_key(p)] = rec
            log.info("worker %d: scoring complete, measured %d/%d", worker_id,
                     sum(bool(rec.get("measured")) for rec in results.values()), len(passed))
            return results
        finally:
            checker.stop()

    def _run_stage(self, proxies, front=None):
        if not proxies:
            return {}
        workers = max(1, min(self.cfg.purity_workers, len(proxies)))
        if workers == 1:
            return self._check_shard(proxies, 0, front)
        import concurrent.futures
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._check_shard, shard, i, front)
                       for i, shard in enumerate(_shard(proxies, workers))]
            for future in concurrent.futures.as_completed(futures):
                results.update(future.result())  # a broken checker fails the round, never fabricates passes
        return results

    def check_all(self, proxies):
        direct = [p for p in proxies if not is_backend(p) or is_udp(p)]
        backends = [p for p in proxies if is_backend(p) and not is_udp(p)]
        results = self._run_stage(direct)
        # Prefer fronts eligible for publication. Scores cannot prevent the
        # backend connectivity test; dependency filtering happens later.
        candidates = [p for p in direct if not is_backend(p) and results[dedup_key(p)].get("connected")]
        scored = apply_history(self.cfg, direct, results)
        eligible, _ = filter_by_purity(self.cfg, candidates, scored)
        eligible_keys = {dedup_key(p) for p in eligible}
        candidates.sort(key=lambda p: (dedup_key(p) not in eligible_keys,
                                       results[dedup_key(p)].get("delay_ms", 10**9)))
        candidates = diversify_fronts(candidates, results)
        self.summary = {"front_candidates": [p["name"] for p in candidates],
                        "connectivity_url": self.cfg.data.get("connectivity_url") or DELAY_URL,
                        "connectivity_timeout_ms": self.cfg.data.get("connectivity_timeout_ms", 10000)}
        pending = backends
        for front in candidates:
            if not pending:
                break
            checked = self._run_stage(pending, front)
            results.update(checked)
            pending = [p for p in pending if not checked[dedup_key(p)].get("connected")]
        for p in backends:
            results.setdefault(dedup_key(p), {"connected": False, "connectivity_attempts": 0,
                               "connectivity_error": "本轮没有测通的前置节点，无法验证后置链路",
                               "measured": False, "score": None, "attempts": 0})
        return results
