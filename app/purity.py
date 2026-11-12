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


def build_mihomo_config(proxies: list[dict], front_name: str | None,
                         mixed_port: int, controller_port: int) -> dict:
    cfg_proxies = []
    for p in proxies:
        node = dict(p)
        # Backend TCP nodes ride the front for the check; UDP backend
        # nodes stay direct (they cannot carry dialer-proxy).
        if "静态" in str(node.get("name", "")) and front_name:
            if str(node.get("type", "")).lower() not in ("hysteria2", "tuic", "wireguard"):
                node["dialer-proxy"] = front_name
        cfg_proxies.append(node)
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
        "rules": ["MATCH,DIRECT"],
    }


class MihomoChecker:
    def __init__(self, config):
        self.cfg = config
        self.proc: subprocess.Popen | None = None
        self.workdir = Path(config.data.get("mihomo_workdir",
                                            "/opt/clash-merge-service/mihomo"))
        self.mixed_port = int(config.data.get("mihomo_mixed_port", 7890))
        self.controller_port = int(config.data.get("mihomo_controller_port", 9090))
        self.info_url = config.data.get("purity_info_url",
                                        "https://my.123169.xyz/v1/info")

    def _controller(self, path: str) -> str:
        return f"http://127.0.0.1:{self.controller_port}{path}"

    def start(self, proxies: list[dict]) -> bool:
        binary = shutil.which(str(self.cfg.data.get("mihomo_bin", "mihomo")))
        if not binary:
            log.warning("purity: mihomo binary not found (%s), skipping check",
                        self.cfg.data.get("mihomo_bin"))
            return False
        front = choose_front(proxies)
        cfg = build_mihomo_config(proxies, front, self.mixed_port,
                                  self.controller_port)
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

    def check_all(self, proxies: list[dict]) -> dict:
        """Returns {dedup_key: purity-record}. Serial, as in the old task."""
        results: dict = {}
        if not self.cfg.data.get("purity_enabled", True):
            log.info("purity: disabled in config, all nodes unmeasured")
        elif not proxies:
            return results
        elif not self.start(proxies):
            log.warning("purity: mihomo unavailable, all nodes unmeasured")
        else:
            try:
                for p in proxies:
                    results[dedup_key(p)] = self.check_one(p)
            finally:
                self.stop()
        for p in proxies:
            results.setdefault(dedup_key(p), {
                "measured": False, "score": None, "exit_ip": None,
                "country": "", "expected_ip": expected_ip(p),
                "match": False, "ippure_url": ippure_url(expected_ip(p)),
                "error": "purity check skipped/unavailable", "attempts": 0})
        return results
