"""Pipeline + scheduler + run state.

Pipeline order (hard requirement):
1. fetch all enabled subscriptions (validated YAML, >0 nodes).
   A single failed source is SKIPPED (recorded in the report as
   ``sources_failed`` with its URL + reason) and the run continues
   with the rest; the run fails only if ALL sources fail. The ini
   fetch below is still fail-fast — the run cannot convert without it.
2. merge + dedup
3. pre-filter by the ini's exclude_remarks (nodes the converter
   would drop anyway are never tested)
4. per-node purity check (no threshold, keep all)
5. availability: nodes failing 3 rounds in a row are omitted from
   the output (still tested next round; back the moment they
   measure again)
6. normalize display names (flag + Chinese country name; static
   nodes rebuilt as "🇺🇸 美国 静态31 1.2.3.4"), then sort by purity
   (measured asc, unmeasured last in original order) and prefix
   names with the measured score ("5% ...")
7. apply the ini (local conversion; remote subconverter opt-in)
8. inject dialer-proxy AFTER conversion, fill backend groups
9. add the low-purity group (measured score <= 15, http excluded)
   to 手动选择 and 国外AI
10. guard: refuse to publish 0 proxies, or fewer than 30% of the
    currently published count (last good file kept, step "guard")
11. atomically replace the output file — ONLY on full success.
    Any failure keeps the last good file and records step + reason.

``reapply_rules`` is a separate, lighter path for when the user
edits the ini on gist: it re-derives groups/rules from the CURRENT
published file with the fresh ini, without fetching subscriptions
or re-testing purity. It cannot restore nodes the pre-filter or
the availability rule removed — only a full round can.

Scheduler: internal asyncio loop, no cron. A threading lock makes
overlapping runs impossible: a scheduled/manual trigger while a run
is in progress is skipped and logged, never run concurrently (a
~270-node serial purity round can take tens of minutes, longer than
a short interval; ``purity_workers`` runs N mihomo instances in
parallel to cut that down).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app import converter as converter_mod
from app.availability import load_availability, update_availability
from app.config import Config
from app.converter import apply_exclude, parse_ini
from app.dialer import (fill_backend_groups, front_group_from_ini,
                        inject_dialer)
from app.fetcher import FetchError, fetch_subscription, fetch_text, make_client
from app.groups import add_low_purity_group, scores_from_names
from app.merge import (dedup_key, merge_proxies, prefix_purity_names,
                       sort_by_purity)
from app.names import normalize_name
from app.notify import failure_text, send_wechat, success_text
from app.purity import MihomoChecker

log = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))

# Output guard: refuse to publish a file whose node count collapsed
# compared with the currently published one (e.g. an upstream returned
# a near-empty subscription, or the checker went haywire). The last
# good file is kept and the run is reported as failed at step "guard".
# Only a drop BELOW this share of the previous count trips it, so the
# normal "one of several sources failed" case still publishes.
GUARD_MIN_RATIO = 0.3
GUARD_MIN_PREVIOUS = 20

# Set by reschedule() so the scheduler re-reads next_run right away
# instead of sleeping out a delay computed from the old interval.
_wake: asyncio.Event | None = None


def _wake_event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


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
        path = _state_file(cfg)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({
            "last_run": state.last_run, "next_run": state.next_run,
            "runs_started": state.runs_started,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - state persistence is best-effort
        log.warning("state persist failed: %s", e)


def _published_proxy_count(out_path: Path) -> int | None:
    """Node count of the currently published file, or None if there
    is none / it cannot be parsed."""
    try:
        if not out_path.exists():
            return None
        doc = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        return len(doc.get("proxies") or [])
    except Exception:  # noqa: BLE001 - guard is advisory on a bad old file
        return None


def _guard_output(cfg: Config, doc: dict, report: dict) -> None:
    """Refuse to publish an empty or collapsed file. Raises
    FetchError("guard", ...); the last good file stays in place."""
    new = len(doc.get("proxies") or [])
    prev = _published_proxy_count(Path(cfg.output_path))
    report["guard_previous_proxies"] = prev
    if new == 0:
        raise FetchError("guard", "new output has 0 proxies, not publishing")
    if (prev is not None and prev >= GUARD_MIN_PREVIOUS
            and new < prev * GUARD_MIN_RATIO):
        raise FetchError(
            "guard", f"new output has {new} proxies vs {prev} published "
            f"(< {int(GUARD_MIN_RATIO * 100)}%), not publishing — "
            "keep the last good file; check the sources")


async def _write_output(cfg: Config, doc: dict) -> None:
    """Write the doc to a temp file, validate it with ``mihomo -t``,
    and atomically replace the published file. A config Clash Verge
    rejects must never overwrite the last good file. If the binary
    is missing entirely, warn and skip validation rather than
    blocking. Raises FetchError("validate", ...) on a failed check.

    The temp file lives next to config.json, NOT in the web root:
    OpenResty serves the output directory statically, and a
    half-written or rejected candidate must never be downloadable.
    It is always removed on failure."""
    out_path = Path(cfg.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.parent / "output.tmp.yaml"
    mihomo_bin = cfg.data.get("mihomo_bin") or "mihomo"
    workdir = cfg.data.get("mihomo_workdir") or "/opt/clash-merge-service/mihomo"

    def _validate() -> subprocess.CompletedProcess:
        return subprocess.run(
            [mihomo_bin, "-t", "-f", str(tmp), "-d", workdir],
            capture_output=True, text=True, timeout=120)

    try:
        tmp.write_text(yaml.safe_dump(doc, allow_unicode=True,
                                      sort_keys=False, width=100000),
                       encoding="utf-8")
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
        try:
            os.replace(tmp, out_path)
        except OSError:
            # Different filesystem (EXDEV): copy next to the target
            # first, then rename there, so the swap stays atomic.
            staged = out_path.with_name("." + out_path.name + ".staged")
            shutil.copyfile(tmp, staged)
            os.replace(staged, out_path)
    finally:
        tmp.unlink(missing_ok=True)


async def reapply_rules(cfg: Config, state: ServiceState) -> dict:
    """Re-apply the CURRENT ini to the CURRENT published file, without
    re-testing purity. For when the user edits the ini on gist and
    does not want to wait for the next hourly round.

    Input is the published file itself, so this can only re-derive
    groups/rules (and the low-purity group, from the scores embedded
    in the names) for nodes that are already in the file. Nodes the
    pre-filter or the availability rule removed are NOT restored by
    a re-apply — only the next full round brings them back. Names
    are left untouched (no re-normalization); the next full round
    applies name changes.
    No WeChat notification: this is a manual admin action and the
    result is shown on the admin page (last_run, kind="reapply").
    The hourly schedule (next_run) is untouched.
    Never raises for pipeline failures — they are recorded in the
    report."""
    if not state.lock.acquire(blocking=False):
        state.runs_skipped_overlap += 1
        return {"ok": False, "skipped_overlap": True,
                "error": "a round is already running"}
    state.running = True
    started = time.time()
    report: dict = {"kind": "reapply", "started_at": _now_cst(),
                    "ok": False}
    step = "load"
    try:
        out_path = Path(cfg.output_path)
        if not out_path.exists():
            raise FetchError("load", "no published file yet — "
                             "run a full round first")
        doc = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        proxies = doc.get("proxies") or []
        if not proxies:
            raise FetchError("load", "published file has no proxies")
        report["proxies_in"] = len(proxies)

        step = "fetch-ini"
        async with make_client(timeout=60) as client:
            ini_text = await fetch_text(cfg.ini_url, client)
            step = "convert"
            doc, cstats = await converter_mod.convert(proxies, ini_text,
                                                      client)
        report.update(cstats)

        step = "dialer-proxy"
        ini = parse_ini(ini_text)
        front_group = front_group_from_ini(ini)
        report["dialer_front_group"] = front_group
        report.update(inject_dialer(doc["proxies"], front_group))
        report["backend_groups_filled"] = fill_backend_groups(
            doc, doc["proxies"])

        step = "low-purity-group"
        report.update(add_low_purity_group(doc, doc["proxies"],
                                           scores_from_names(doc["proxies"])))

        step = "write"
        await _write_output(cfg, doc)
        report["output_path"] = str(out_path)

        report["ok"] = True
        report["duration_seconds"] = round(time.time() - started, 1)
        report["finished_at"] = _now_cst()
    except FetchError as e:
        report.update({"ok": False, "failed_step": e.step,
                       "error": e.reason, "finished_at": _now_cst(),
                       "duration_seconds": round(time.time() - started, 1)})
        log.error("reapply failed at %s: %s", e.step, e.reason)
    except Exception as e:  # noqa: BLE001 - any failure keeps last good file
        report.update({"ok": False, "failed_step": step,
                       "error": str(e)[:500], "finished_at": _now_cst(),
                       "duration_seconds": round(time.time() - started, 1)})
        log.exception("reapply failed at %s", step)
    finally:
        state.running = False
        state.lock.release()
        state.last_run = report
        state.last_error = "" if report["ok"] else (
            f"{report.get('failed_step')}: {report.get('error')}")
        persist_state(cfg, state)
    return report


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
            # Sources are fetched concurrently (one slow airport no
            # longer serializes the rest); results keep config order.
            results = await asyncio.gather(
                *(fetch_subscription(s["url"], client) for s in subs),
                return_exceptions=True)
            fetched: list[tuple[str, list[dict]]] = []
            failed: list[dict] = []
            for s, res in zip(subs, results):
                if isinstance(res, BaseException):
                    if not isinstance(res, Exception):
                        raise res  # CancelledError etc.: do not swallow
                    # Per-source tolerance: skip this source, keep going
                    # with the rest. The URL + reason go into the report
                    # (admin page shows them; the WeChat notification
                    # only ever shows the counts, never the address).
                    reason = (res.reason if isinstance(res, FetchError)
                              else f"{type(res).__name__}: {res}"[:300])
                    failed.append({"url": s["url"], "error": reason})
                    log.warning("fetch: source %s (%s) failed, skipping: %s",
                                s.get("id"), s["url"], reason)
                    continue
                fetched.append((s.get("id", "?"), res))
                log.info("fetch: %s -> %d nodes", s.get("id"), len(res))
            report["sources_ok"] = len(fetched)
            report["sources_failed"] = failed
            if not fetched:
                raise FetchError(
                    "fetch",
                    "all %d subscription sources failed: %s"
                    % (len(subs), "; ".join(f["error"] for f in failed)))

            step = "merge"
            merged, mstats = merge_proxies(fetched)
            report.update(mstats)

            step = "fetch-ini"
            # Fetch the ini early: its exclude_remarks decides which
            # nodes are worth testing at all. Same matching as the
            # converter (apply_exclude, re.search on the name), so a
            # node dropped here is one the converter would drop
            # later anyway — with the current ini (no ^ anchors) the
            # two agree exactly. The converter still runs its own
            # apply_exclude as a safety net.
            ini_text = await fetch_text(cfg.ini_url, client)
            step = "prefilter"
            ini = parse_ini(ini_text)
            # Match on the normalized name (what the converter and
            # reapply see once the score prefix is stripped — see
            # converter.exclude_match_name), not the raw airport name.
            to_test, pre_excluded = apply_exclude(
                merged, ini,
                name_of=lambda p: normalize_name(str(p.get("name", "")), None))
            report["prefilter_excluded"] = pre_excluded
            kept_ids = {id(p) for p in to_test}
            report["prefilter_excluded_names"] = [
                str(p.get("name", "")) for p in merged
                if id(p) not in kept_ids]
            log.info("prefilter: %d nodes in, %d excluded by "
                     "exclude_remarks, %d to test",
                     len(merged), pre_excluded, len(to_test))

            step = "purity"
            report["purity_workers"] = cfg.purity_workers
            checker = MihomoChecker(cfg)
            purity = await asyncio.to_thread(checker.check_all, to_test)
            measured = sum(1 for r in purity.values() if r.get("measured"))
            report["purity_measured"] = measured
            report["purity_unmeasured"] = len(to_test) - measured
            report["purity_mismatched_exit_ip"] = sum(
                1 for r in purity.values()
                if r.get("measured") and r.get("match") is False)
            # Delay screen stats: how many nodes failed the screen,
            # and how many of those the short info attempt still
            # measured (the screen's false negatives).
            screen_failed = [r for r in purity.values()
                             if r.get("delay_screen") == "fail"]
            report["purity_delay_screen_failed"] = len(screen_failed)
            report["purity_delay_screen_rescued"] = sum(
                1 for r in screen_failed if r.get("measured"))

            step = "availability"
            # Nodes that failed 3 rounds in a row are omitted from
            # this round's output (they are still tested next round
            # and come back the moment they measure again).
            records = load_availability(cfg)
            kept, astats = update_availability(cfg, records, to_test, purity)
            report.update(astats)

            step = "normalize-names"
            # Uniform display names: flag + Chinese country name,
            # static nodes rebuilt as "🇺🇸 美国 静态31 1.2.3.4".
            # The purity record's exit country is only a fallback
            # for names carrying no recognizable country. Runs on
            # the merged dicts; dedup keys exclude names, so the
            # purity lookup below still works.
            for p in kept:
                rec = purity.get(dedup_key(p))
                country = (rec or {}).get("country") or None
                p["name"] = normalize_name(str(p.get("name", "")), country)

            ordered = sort_by_purity(kept, purity)
            # Display names carry the measured purity score, e.g.
            # "5% 🇹🇼 日本02 三网高速"; unmeasured nodes keep their
            # (normalized) name.
            ordered, nstats = prefix_purity_names(ordered, purity)
            report.update(nstats)

            step = "convert"
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
                # Keep the remote doc as-is: its groups reference the
                # remote proxy list (which may have dropped nodes per
                # the ini), so swapping in our list would desync them.
                if cstats["proxies_out"] != len(ordered):
                    log.info("remote convert: %d proxies in, %d out",
                             len(ordered), cstats["proxies_out"])
            else:
                doc, cstats = await converter_mod.convert(ordered, ini_text, client)
            report.update(cstats)

            step = "dialer-proxy"
            # The front group name comes from the ini (see
            # front_group_from_ini) so renaming it on gist just works.
            front_group = front_group_from_ini(ini)
            report["dialer_front_group"] = front_group
            dstats = inject_dialer(doc["proxies"], front_group)
            report.update(dstats)
            filled = fill_backend_groups(doc, doc["proxies"])
            report["backend_groups_filled"] = filled

            step = "low-purity-group"
            # Nodes with a measured score <= 15 (http nodes excluded)
            # go into 🎯 纯净优选 (url-test), which is also added to
            # 🚀 手动选择 and 🤖 国外AI. Scores are looked up by final
            # display name (dedup keys exclude names, so the lookup
            # survives renaming).
            scores_by_name = {}
            for p in ordered:
                rec = purity.get(dedup_key(p))
                if rec and rec.get("measured") and rec.get("score") is not None:
                    scores_by_name[str(p.get("name", ""))] = int(rec["score"])
            report.update(add_low_purity_group(doc, doc["proxies"],
                                               scores_by_name))

            step = "guard"
            _guard_output(cfg, doc, report)

            step = "write"
            await _write_output(cfg, doc)
            report["output_path"] = str(Path(cfg.output_path))

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
        # A manual run moved next_run: let the scheduler re-read it.
        _wake_event().set()

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
    # Wake the scheduler so it sleeps against the NEW next_run (an
    # interval shortened on /admin used to wait out the old delay).
    _wake_event().set()


async def scheduler_loop(cfg: Config, state: ServiceState) -> None:
    """Internal scheduler: sleep until next_run (or until reschedule()
    wakes it), then run. No cron. Never dies: any error in one
    iteration is logged and the loop carries on."""
    wake = _wake_event()
    while True:
        try:
            try:
                nxt = datetime.strptime(state.next_run, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            except Exception:
                nxt = datetime.now(CST) + timedelta(seconds=cfg.interval_seconds)
                state.next_run = nxt.strftime("%Y-%m-%d %H:%M:%S")
            delay = (nxt - datetime.now(CST)).total_seconds()
            if delay > 0:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                    continue  # rescheduled: recompute against new next_run
                except asyncio.TimeoutError:
                    pass
            if state.running:
                # A manual run is in progress; its finally block sets
                # a fresh next_run. Re-check shortly.
                await asyncio.sleep(30)
                continue
            await run_pipeline(cfg, state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the scheduler must survive
            log.exception("scheduler: iteration failed, retrying in 60s")
            await asyncio.sleep(60)
