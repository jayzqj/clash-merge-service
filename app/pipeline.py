"""Pipeline + scheduler + run state.

Pipeline order (hard requirement):
1. fetch all enabled subscriptions (validated YAML, >0 nodes)
2. merge + dedup
3. per-node purity check (no threshold, keep all)
4. sort by purity (measured asc, unmeasured last in original order)
5. apply the ini (local conversion; remote subconverter opt-in)
6. inject dialer-proxy AFTER conversion, fill backend groups
7. atomically replace the output file — ONLY on full success.
   Any failure keeps the last good file and records step + reason.

Scheduler: internal asyncio loop, no cron. A threading lock makes
overlapping runs impossible: a scheduled/manual trigger while a run
is in progress is skipped and logged, never run concurrently (a
~270-node serial purity round can take tens of minutes, longer than
a short interval).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app import converter as converter_mod
from app.config import Config
from app.dialer import fill_backend_groups, inject_dialer
from app.fetcher import FetchError, fetch_subscription, fetch_text, make_client
from app.merge import merge_proxies, sort_by_purity
from app.notify import failure_text, send_wechat, success_text
from app.purity import MihomoChecker

log = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))


def _now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ServiceState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    last_run: dict | None = None
    next_run: str | None = None
    last_error: str = ""
    runs_started: int = 0
    runs_skipped_overlap: int = 0

    def status(self) -> dict:
        return {
            "running": self.running,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "runs_started": self.runs_started,
            "runs_skipped_overlap": self.runs_skipped_overlap,
        }


def _state_file(cfg: Config) -> Path:
    return cfg.path.parent / "state.json"


def load_state(cfg: Config) -> ServiceState:
    state = ServiceState()
    try:
        raw = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        state.last_run = raw.get("last_run")
        state.next_run = raw.get("next_run")
        state.runs_started = raw.get("runs_started", 0)
    except Exception:
        pass
    if not state.next_run:
        state.next_run = (datetime.now(CST) +
                          timedelta(seconds=cfg.interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    return state


def persist_state(cfg: Config, state: ServiceState) -> None:
    try:
        _state_file(cfg).write_text(json.dumps({
            "last_run": state.last_run, "next_run": state.next_run,
            "runs_started": state.runs_started,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - state persistence is best-effort
        log.warning("state persist failed: %s", e)


async def run_pipeline(cfg: Config, state: ServiceState) -> dict:
    """Run one full round. Returns a report dict. Never raises for
    pipeline failures — they are recorded in the report and notified."""
    if not state.lock.acquire(blocking=False):
        state.runs_skipped_overlap += 1
        log.warning("pipeline: previous round still running, skipped this trigger")
        return {"ok": False, "skipped_overlap": True,
                "error": "previous round still running"}
    state.running = True
    state.runs_started += 1
    started = time.time()
    report: dict = {"started_at": _now_cst(), "ok": False}
    step = "fetch"
    try:
        subs = cfg.enabled_subscriptions
        report["sources_total"] = len(subs)
        if not subs:
            raise FetchError("fetch", "no enabled subscriptions in config")
        async with make_client(timeout=60) as client:
            fetched: list[tuple[str, list[dict]]] = []
            for s in subs:
                proxies = await fetch_subscription(s["url"], client)
                fetched.append((s.get("id", "?"), proxies))
                log.info("fetch: %s -> %d nodes", s.get("id"), len(proxies))
            report["sources_ok"] = len(fetched)

            step = "merge"
            merged, mstats = merge_proxies(fetched)
            report.update(mstats)

            step = "purity"
            checker = MihomoChecker(cfg)
            purity = await asyncio.to_thread(checker.check_all, merged)
            measured = sum(1 for r in purity.values() if r.get("measured"))
            report["purity_measured"] = measured
            report["purity_unmeasured"] = len(merged) - measured
            report["purity_mismatched_exit_ip"] = sum(
                1 for r in purity.values()
                if r.get("measured") and r.get("match") is False)
            ordered = sort_by_purity(merged, purity)

            step = "convert"
            ini_text = await fetch_text(cfg.ini_url, client)
            if cfg.data.get("use_remote_subconverter"):
                merged_yaml = yaml.safe_dump(
                    {"proxies": ordered}, allow_unicode=True, sort_keys=False)
                doc = await converter_mod.remote_convert(
                    merged_yaml, cfg.ini_url, cfg.subconverter_base,
                    cfg.data.get("merged_public_url", ""), client)
                cstats = {"proxies_out": len(doc.get("proxies", [])),
                          "groups": len(doc.get("proxy-groups", [])),
                          "rules": len(doc.get("rules", [])),
                          "rule_providers": len(doc.get("rule-providers", {})),
                          "excluded_by_ini": 0, "proxies_in": len(ordered)}
                doc["proxies"] = ordered if len(doc.get("proxies", [])) != len(ordered) else doc["proxies"]
            else:
                doc, cstats = await converter_mod.convert(ordered, ini_text, client)
            report.update(cstats)

            step = "dialer-proxy"
            dstats = inject_dialer(doc["proxies"])
            report.update(dstats)
            filled = fill_backend_groups(doc, doc["proxies"])
            report["backend_groups_filled"] = filled

            step = "write"
            out_path = Path(cfg.output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(doc, allow_unicode=True,
                                          sort_keys=False, width=100000),
                           encoding="utf-8")

            # Validate the new file with mihomo before publishing it:
            # a config Clash Verge rejects (e.g. an unsupported rule
            # type) must never overwrite the last good file. If the
            # binary is missing entirely, warn and skip validation
            # rather than blocking the run.
            step = "validate"
            mihomo_bin = cfg.data.get("mihomo_bin") or "mihomo"
            workdir = cfg.data.get("mihomo_workdir") or "/opt/clash-merge-service/mihomo"

            def _validate() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [mihomo_bin, "-t", "-f", str(tmp), "-d", workdir],
                    capture_output=True, text=True, timeout=120)

            try:
                res = await asyncio.to_thread(_validate)
            except FileNotFoundError:
                log.warning("pipeline: mihomo binary %r not found, "
                            "skipping -t validation", mihomo_bin)
            else:
                if res.returncode != 0:
                    tail = ((res.stderr or "") + (res.stdout or ""))[-500:]
                    raise FetchError("validate",
                                     f"mihomo -t failed: {tail.strip()}")
                log.info("pipeline: mihomo -t passed for new output")
            os.replace(tmp, out_path)
            report["output_path"] = str(out_path)

        report["ok"] = True
        report["duration_seconds"] = round(time.time() - started, 1)
        report["finished_at"] = _now_cst()
    except FetchError as e:
        report.update({"ok": False, "failed_step": e.step,
                       "error": e.reason, "finished_at": _now_cst(),
                       "duration_seconds": round(time.time() - started, 1)})
        log.error("pipeline failed at %s: %s", e.step, e.reason)
    except Exception as e:  # noqa: BLE001 - any failure keeps last good file
        report.update({"ok": False, "failed_step": step,
                       "error": str(e)[:500], "finished_at": _now_cst(),
                       "duration_seconds": round(time.time() - started, 1)})
        log.exception("pipeline failed at %s", step)
    finally:
        state.running = False
        state.lock.release()
        state.last_run = report
        if not report["ok"]:
            state.last_error = f"{report.get('failed_step')}: {report.get('error')}"
        else:
            state.last_error = ""
        state.next_run = (datetime.now(CST) +
                          timedelta(seconds=cfg.interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        persist_state(cfg, state)

    # Notify (master switch; success AND failure, never node-change alone).
    if cfg.notify_enabled and cfg.wechat_webhook:
        if report["ok"]:
            await send_wechat(cfg.wechat_webhook, success_text(report))
        else:
            await send_wechat(cfg.wechat_webhook, failure_text(
                str(report.get("failed_step", step)),
                str(report.get("error", ""))))
    return report


def reschedule(state: ServiceState, cfg: Config) -> None:
    state.next_run = (datetime.now(CST) +
                      timedelta(seconds=cfg.interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    persist_state(cfg, state)


async def scheduler_loop(cfg: Config, state: ServiceState) -> None:
    """Internal scheduler: sleep until next_run, then run. No cron."""
    while True:
        try:
            nxt = datetime.strptime(state.next_run, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except Exception:
            nxt = datetime.now(CST) + timedelta(seconds=cfg.interval_seconds)
            state.next_run = nxt.strftime("%Y-%m-%d %H:%M:%S")
        delay = max(1.0, (nxt - datetime.now(CST)).total_seconds())
        await asyncio.sleep(delay)
        if not state.running:
            await run_pipeline(cfg, state)
