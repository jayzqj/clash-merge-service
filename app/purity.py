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
ascending, unmeasured last in original order).

Backend (静态) nodes are checked DIRECT from the VPS by default (no
``dialer-proxy`` in the temporary check config). Only the CHECK is
direct — the published output still gives backend TCP nodes
``dialer-proxy: <ini 前置 group>`` (see ``app/dialer.py``).

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

Why direct: backend nodes are Bright Data superproxy endpoints
(``http`` to ``brd.superproxy.io:44445``) and the gateway rejects
airport-front exit IPs (TLS EOF through LA hysteria2 / Japan
vless fronts, verified 2026-11-12). The VPS's own IP is accepted:
re-verified 2026-11-14, 170/171 static nodes measured direct from
the VPS with scores identical to the published file (the one miss
was a transient TLS EOF). The check config sets ``ipv6: false``
because the gateway is only reachable over IPv4 from the VPS.

Optional check front (``check_front`` = a full proxy dict in the
config file, never committed; ``check_front_enabled`` = switch,
default off): when BOTH are set, backend TCP checks ride that front
via ``dialer-proxy`` instead of going direct. Useful only if the
VPS IP ever stops being accepted. There is no automatic fallback to
an airport front any more — that path never measured anything.

Delay screen (``purity_delay_screen``, default on): before the
per-node loop each worker runs mihomo's concurrent group delay test
over its shard. Nodes that pass get the normal check (3 attempts,
30 s each). Nodes that fail both delay rounds get ONE info attempt
with a 15 s timeout — a dead node costs ~15 s instead of up to
~94 s, and a node the delay test wrongly failed is still measured.
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

from app.config import MAX_PURITY_WORKERS as MAX_WORKERS
from app.merge import dedup_key, expected_ip

log = logging.getLogger(__name__)

SELECTOR_GROUP = "MERGE-CHECK"
# Name the check front gets inside the test config (never collides
# with a subscription node's name).
FRONT_INTERNAL_NAME = "__check_front__"
UDP_TYPES = ("hysteria2", "tuic", "wireguard")
MAX_ATTEMPTS = 3
RETRY_SLEEP = 2
INFO_TIMEOUT = 30

# Delay screen (see MihomoChecker.screen_alive). Measured on the VPS
# 2026-11-14 over 307 nodes: one group delay test takes its timeout
# (10 s); 60/60 sampled nodes that passed it also measured; but a
# FAILED delay test is not proof of death — with a 5 s timeout 13 of
# 22 delay-failed nodes still measured fine, with 10 s only 10 nodes
# failed both rounds. So the screen is a hint, never a verdict: a
# node that fails it still gets ONE info attempt with a short timeout
# instead of MAX_ATTEMPTS x INFO_TIMEOUT.
DELAY_URL = "https://cp.cloudflare.com/generate_204"
DELAY_TIMEOUT_MS = 10000
DELAY_ROUNDS = 2
SCREEN_FAIL_TIMEOUT = 15
# Below this pass share the screen itself is considered broken (test
# URL down, mihomo overloaded) and is ignored for the shard.
SCREEN_MIN_PASS_RATIO = 0.2
SCREEN_MIN_NODES = 5


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
        # Optional dedicated front for backend checks: used only when
        # check_front is a full proxy dict AND check_front_enabled is
        # on. Default: backend nodes are checked direct.
        front = config.data.get("check_front") or None
        enabled = bool(config.data.get("check_front_enabled", False))
        self.check_front: dict | None = (
            front if (enabled and isinstance(front, dict)) else None)

    def _controller(self, path: str) -> str:
        return f"http://127.0.0.1:{self.controller_port}{path}"

    def start(self, proxies: list[dict]) -> bool:
        binary = shutil.which(str(self.cfg.data.get("mihomo_bin", "mihomo")))
        if not binary:
            log.warning("purity: mihomo binary not found (%s), skipping check",
                        self.cfg.data.get("mihomo_bin"))
            return False
        if self.check_front:
            # Renamed internally: a check front sharing its name with
            # a subscription node would make mihomo refuse the config
            # (duplicate proxy name) and lose the whole round.
            front_name: str | None = FRONT_INTERNAL_NAME
            extra = [dict(self.check_front, name=FRONT_INTERNAL_NAME)]
            log.info("purity worker %d: backend checks ride configured "
                     "front %s", self.worker_id,
                     self.check_front.get("name", ""))
        else:
            front_name = None
            extra = []
        cfg = build_mihomo_config(proxies, front_name, self.mixed_port,
                                  self.controller_port, extra_proxies=extra)
        self.workdir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.workdir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True,
                                           sort_keys=False), encoding="utf-8")
        # The child keeps its own copy of the fd; close ours right
        # away so every round does not leak one file handle.
        with open(self.workdir / "mihomo.log", "a", encoding="utf-8") as logfile:
            self.proc = subprocess.Popen(
                [binary, "-f", str(cfg_path), "-d", str(self.workdir)],
                stdout=logfile, stderr=subprocess.STDOUT,
                start_new_session=True)
        for _ in range(30):
            if self.proc.poll() is not None:
                log.warning("purity worker %d: mihomo exited early (code %s), "
                            "see %s", self.worker_id, self.proc.returncode,
                            self.workdir / "mihomo.log")
                self.proc = None
                return False
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

    def _info_once(self, timeout: float = INFO_TIMEOUT) -> dict:
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        with httpx.Client(proxy=proxy, timeout=timeout,
                          trust_env=False) as client:
            r = client.get(self.info_url,
                           headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()

    def screen_alive(self, proxies: list[dict]) -> set[str] | None:
        """Delay screen: one concurrent delay test over the whole
        selector group (a few seconds for a full shard), repeated
        DELAY_ROUNDS times; a node passing ANY round is 'alive'.

        Returns the set of alive node names, or None when the screen
        is disabled / errored / implausible — the caller then checks
        every node the normal way. Note mihomo answers 504 when every
        proxy times out, which lands in the error path on purpose."""
        if not self.cfg.data.get("purity_delay_screen", True):
            return None
        url = str(self.cfg.data.get("purity_delay_url") or DELAY_URL)
        names = [str(p["name"]) for p in proxies]
        alive: set[str] = set()
        try:
            for _ in range(DELAY_ROUNDS):
                r = httpx.get(
                    self._controller(f"/group/{SELECTOR_GROUP}/delay"),
                    params={"url": url, "timeout": DELAY_TIMEOUT_MS},
                    timeout=DELAY_TIMEOUT_MS / 1000 + 20, trust_env=False)
                r.raise_for_status()
                data = r.json()
                alive.update(n for n in names
                             if isinstance(data.get(n), int) and data[n] > 0)
                if len(alive) == len(names):
                    break
        except Exception as e:  # noqa: BLE001 - screen is optional
            log.warning("purity worker %d: delay screen failed (%s), "
                        "checking every node normally", self.worker_id,
                        f"{type(e).__name__}: {e}"[:200])
            return None
        if (len(names) >= SCREEN_MIN_NODES
                and len(alive) < len(names) * SCREEN_MIN_PASS_RATIO):
            log.warning("purity worker %d: delay screen passed only %d/%d "
                        "nodes — ignoring it, checking every node normally",
                        self.worker_id, len(alive), len(names))
            return None
        log.info("purity worker %d: delay screen %d/%d alive",
                 self.worker_id, len(alive), len(names))
        return alive

    def check_one(self, proxy: dict, attempts: int = MAX_ATTEMPTS,
                  timeout: float = INFO_TIMEOUT) -> dict:
        exp_ip = expected_ip(proxy)
        last_err = ""
        for attempt in range(attempts):
            try:
                self._select(str(proxy["name"]))
                data = self._info_once(timeout)
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
                if attempt < attempts - 1:
                    time.sleep(RETRY_SLEEP)
        return {"measured": False, "score": None, "exit_ip": None,
                "country": "", "expected_ip": exp_ip, "match": False,
                "ippure_url": ippure_url(exp_ip), "error": last_err,
                "attempts": attempts}

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
            alive = checker.screen_alive(shard)
            for p in shard:
                if alive is None or str(p["name"]) in alive:
                    rec = checker.check_one(p)
                    if alive is not None:
                        rec["delay_screen"] = "pass"
                else:
                    # Failed the delay screen: probably dead, but the
                    # screen has false negatives, so it still gets one
                    # short info attempt before counting as a failure.
                    rec = checker.check_one(p, attempts=1,
                                            timeout=SCREEN_FAIL_TIMEOUT)
                    rec["delay_screen"] = "fail"
                if checker.proc is not None and checker.proc.poll() is not None:
                    # mihomo died mid-shard (e.g. OOM-killed): this node
                    # and the rest of the shard were not really tested.
                    # Leave them out -> check_all marks them attempts=0,
                    # so they do not count as availability failures.
                    log.warning("purity worker %d: mihomo exited (code %s) "
                                "mid-shard, %d nodes left unmeasured",
                                worker_id, checker.proc.returncode,
                                len(shard) - len(results))
                    checker.proc = None
                    break
                results[dedup_key(p)] = rec
        finally:
            checker.stop()
        return results

    def probe_info(self) -> str:
        """Direct (no proxy) request to the info endpoint before a
        round. Returns "" if it answers with a fraudScore, else the
        error. If the endpoint itself is down or rate-limiting, every
        node would 'fail' — that must not count against the nodes."""
        last = ""
        for attempt in range(2):
            try:
                with httpx.Client(timeout=15, trust_env=False) as client:
                    r = client.get(self.info_url,
                                   headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    if r.json().get("fraudScore") is None:
                        raise RuntimeError("no fraudScore in response")
                    return ""
            except Exception as e:  # noqa: BLE001 - reported to the caller
                last = f"{type(e).__name__}: {e}"[:200]
                if attempt == 0:
                    time.sleep(RETRY_SLEEP)
        return last

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
        skip_reason = "purity check skipped/unavailable"
        probe_err = ""
        if not self.cfg.data.get("purity_enabled", True):
            log.info("purity: disabled in config, all nodes unmeasured")
        elif not proxies:
            return results
        elif (probe_err := self.probe_info()):
            # Info endpoint unreachable from the VPS itself: nothing
            # this round could measure. All nodes stay attempts=0.
            log.warning("purity: info endpoint %s unreachable directly "
                        "(%s) — skipping this round's checks, nodes "
                        "left unmeasured and not counted as failures",
                        self.info_url, probe_err)
            skip_reason = f"info endpoint unreachable: {probe_err}"
        else:
            workers = int(self.cfg.data.get("purity_workers", 1) or 1)
            workers = max(1, min(workers, MAX_WORKERS, len(proxies)))
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
                "error": skip_reason, "attempts": 0})
        return results
