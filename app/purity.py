"""Per-node purity check, reusing the existing 3-hourly task's method.

Method (from the existing task, ``check_nodes.py`` / ``run_all.py``):
- Build a temporary mihomo config from the merged proxies.
- Start mihomo (only for the duration of the round, then stop it —
  the VPS has only 2 GB RAM and mihomo must not stay resident).
- For each node, serially: switch the selector group to that node
  via the external controller (PUT /proxies/<group>), then query
  ``https://my.123169.xyz/v1/info`` through mihomo's mixed port and
  record exit IP / countryCode / fraudScore. Verify the returned exit
  IP matches the node's expected IP; a mismatch is flagged, not
  silently accepted. 3 attempts per node.
- Nodes are keyed by IP (dedup key in this module), never by country
  label.

Differences from the old task: NO ≤15% threshold and NO filtering —
every node is kept; purity only orders the output (measured by score
ascending, unmeasured last in original order). Backend (静态) TCP
nodes are checked through a front node via ``dialer-proxy`` (a
concrete front proxy name, chosen from the merged list — see
``choose_front``); UDP backend nodes cannot carry dialer-proxy, so
they are checked direct and will usually end up unmeasured if they
need the chain (kept anyway).

If mihomo is unavailable or ``purity_enabled`` is off, every node is
returned unmeasured and the pipeline continues — purity must never
block a merge. On this dev VM UDP is blocked, so hysteria2/tuic
checks fail here; on the VPS (UDP open) they are expected to work.

Parallel workers (``purity_workers``, default 1 = serial): the check
is serial by default because all nodes share ONE selector group —
two threads selecting different nodes would race. With N workers,
each worker gets its OWN mihomo instance (own mixed/controller
ports, own workdir, own selector group) and a round-robin shard of
the nodes; workers run in a thread pool and their results are merged.
Each worker is an independent ``MihomoChecker``; nothing is shared
between them except the read-only config.

Dedicated check front (``check_front``, a full proxy dict in the
config file, never committed): backend (静态) nodes are Bright Data
superproxy endpoints (``http`` to ``brd.superproxy.io:44445``) and
the gateway only accepts the chain when the connecting IP is one it
accepts. Verified 2026-11-12 on the VPS: through airport fronts
(LA hysteria2 exit 198.255.5.154, Japan vless/hysteria2 exit
103.62.49.130) every static check fails with TLS EOF — direct,
through a front, and with the old task's hosts override; through
the user's own Racknerd anytls node (exit = VPS IP 23.238.2.198)
the same nodes measure fine and match the old 3-hourly task exactly
(US-静态01 57%, US-静态02 28%, US-静态03 20%). So when
``check_front`` is configured, ALL backend checks in every worker
ride that front; without it, each worker falls back to
``choose_front`` on its own shard (airport fronts — statics will
usually end up unmeasured).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from app.merge import dedup_key, expected_ip

log = logging.getLogger(__name__)

SELECTOR_GROUP = "MERGE-CHECK"
FRONT_PREFERENCE = ("hysteria2", "anytls", "vless", "vmess", "trojan", "ss")


def ippure_url(ip: str | None) -> str:
    return f"https://ippure.com/?ip={ip}" if ip else "https://ippure.com/"


def choose_front(proxies: list[dict]) -> str | None:
    """Pick a concrete front proxy for backend nodes: first
    non-backend node, preferring hysteria2, then anytls, etc."""
    candidates = [p for p in proxies if "静态" not in str(p.get("name", ""))]
    for pref in FRONT_PREFERENCE:
        for p in candidates:
            if str(p.get("type", "")).lower() == pref:
                return str(p["name"])
    return str(candidates[0]["name"]) if candidates else None


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
        # Backend TCP nodes ride the front for the check; UDP backend
        # nodes stay direct (they cannot carry dialer-proxy).
        if "静态" in str(node.get("name", "")) and front_name:
            if str(node.get("type", "")).lower() not in ("hysteria2", "tuic", "wireguard"):
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
        # Dedicated front for backend checks (full proxy dict from
        # the config file, or None). See the module docstring: the
        # Bright Data gateway rejects airport-front exit IPs.
        front = config.data.get("check_front") or None
        self.check_front: dict | None = front if isinstance(front, dict) else None

    def _controller(self, path: str) -> str:
        return f"http://127.0.0.1:{self.controller_port}{path}"

    def start(self, proxies: list[dict]) -> bool:
        binary = shutil.which(str(self.cfg.data.get("mihomo_bin", "mihomo")))
        if not binary:
            log.warning("purity: mihomo binary not found (%s), skipping check",
                        self.cfg.data.get("mihomo_bin"))
            return False
        if self.check_front:
            front_name = str(self.check_front.get("name", ""))
            extra = [self.check_front]
            log.info("purity worker %d: backend checks ride configured "
                     "front %s", self.worker_id, front_name)
        else:
            front_name = choose_front(proxies)
            extra = []
        cfg = build_mihomo_config(proxies, front_name, self.mixed_port,
                                  self.controller_port, extra_proxies=extra)
        self.workdir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.workdir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True,
                                           sort_keys=False), encoding="utf-8")
        logfile = open(self.workdir / "mihomo.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [binary, "-f", str(cfg_path), "-d", str(self.workdir)],
            stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(30):
            try:
                r = httpx.get(self._controller("/version"), timeout=3,
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
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
            self.proc = None

    def _select(self, name: str) -> None:
        r = httpx.put(self._controller(f"/proxies/{SELECTOR_GROUP}"),
                      json={"name": name}, timeout=10, trust_env=False)
        r.raise_for_status()

    def _info_once(self) -> dict:
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        with httpx.Client(proxy=proxy, timeout=30, trust_env=False) as client:
            r = client.get(self.info_url,
                           headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()

    def check_one(self, proxy: dict) -> dict:
        exp_ip = expected_ip(proxy)
        last_err = ""
        for attempt in range(3):
            try:
                self._select(str(proxy["name"]))
                data = self._info_once()
                got_ip = data.get("ip")
                score = data.get("fraudScore")
                if score is None:
                    raise RuntimeError(f"no fraudScore in {data}")
                country = data.get("countryCode", "")
                return {"measured": True, "score": int(score),
                        "exit_ip": got_ip, "country": country,
                        "expected_ip": exp_ip,
                        "match": (got_ip == exp_ip) if exp_ip else None,
                        "ippure_url": ippure_url(got_ip),
                        "attempts": attempt + 1}
            except Exception as e:  # noqa: BLE001 - retry any failure
                last_err = str(e)[:200]
                time.sleep(2)
        return {"measured": False, "score": None, "exit_ip": None,
                "country": "", "expected_ip": exp_ip, "match": False,
                "ippure_url": ippure_url(exp_ip), "error": last_err,
                "attempts": 3}

    def _check_shard(self, shard: list[dict], worker_id: int) -> dict:
        """Check one shard with a dedicated mihomo instance. Runs in
        its own thread; the checker instance is never shared."""
        checker = MihomoChecker(self.cfg, worker_id=worker_id)
        results: dict = {}
        if not checker.start(shard):
            log.warning("purity worker %d: mihomo unavailable, %d nodes "
                        "unmeasured", worker_id, len(shard))
            return results
        try:
            for p in shard:
                results[dedup_key(p)] = checker.check_one(p)
        finally:
            checker.stop()
        return results

    def check_all(self, proxies: list[dict]) -> dict:
        """Returns {dedup_key: purity-record}.

        ``purity_workers`` (default 1) controls parallelism: 1 runs
        the old serial path; N > 1 splits the nodes round-robin
        across N workers, each with its own mihomo instance, and
        merges the results. A worker that fails to start only loses
        its own shard (those nodes come back unmeasured); it never
        aborts the other workers.
        """
        results: dict = {}
        if not self.cfg.data.get("purity_enabled", True):
            log.info("purity: disabled in config, all nodes unmeasured")
        elif not proxies:
            return results
        else:
            workers = int(self.cfg.data.get("purity_workers", 1) or 1)
            workers = max(1, min(workers, 16, len(proxies)))
            if workers == 1:
                results = self._check_shard(proxies, 0)
            else:
                import concurrent.futures
                shards = _shard(proxies, workers)
                log.info("purity: %d workers, shards %s",
                         workers, [len(s) for s in shards])
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=workers) as pool:
                    futs = [pool.submit(self._check_shard, shard, i)
                            for i, shard in enumerate(shards)]
                    for fut in concurrent.futures.as_completed(futs):
                        try:
                            results.update(fut.result())
                        except Exception as e:  # noqa: BLE001 - one shard must not kill the round
                            log.warning("purity: worker failed: %s", e)
        for p in proxies:
            results.setdefault(dedup_key(p), {
                "measured": False, "score": None, "exit_ip": None,
                "country": "", "expected_ip": expected_ip(p),
                "match": False, "ippure_url": ippure_url(expected_ip(p)),
                "error": "purity check skipped/unavailable", "attempts": 0})
        return results
