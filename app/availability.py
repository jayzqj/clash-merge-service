"""Per-node availability history.

A node is "unavailable" in a round when it was actually tested and
the purity check failed after its retries (``measured`` is False and
``attempts`` > 0). Nodes that were never attempted (purity disabled,
mihomo unavailable, or a worker that failed to start) do NOT count:
a dead checker must not nuke the whole subscription.

Consecutive failures are counted per node, keyed by the same
``dedup_key`` used for merging (whole proxy dict minus name), so a
rename does not reset the count. When a node reaches
``FAIL_THRESHOLD`` (3) consecutive failures it is omitted from the
final output — but it is still fetched and still tested every round,
so the moment a later test measures it again the counter resets to 0
and the node comes back.

Records live in ``availability.json`` next to ``config.json`` /
``state.json`` (never committed; it holds node names and failure
counts only, no credentials). Records not seen for ``PRUNE_DAYS``
are dropped on save so the file does not grow forever.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.merge import dedup_key

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
FAIL_THRESHOLD = 3
PRUNE_DAYS = 30


def _now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def availability_file(cfg) -> Path:
    return cfg.path.parent / "availability.json"


def load_availability(cfg) -> dict:
    """Returns {dedup_key: {"fails": int, "name": str, "last_seen": str}}."""
    try:
        raw = json.loads(availability_file(cfg).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items()
                    if isinstance(v, dict) and isinstance(v.get("fails"), int)}
    except Exception:
        pass
    return {}


def save_availability(cfg, records: dict) -> dict:
    """Persist records, pruning entries not seen for PRUNE_DAYS.
    Returns the records that were actually kept (pruned ones are
    gone from disk and from the returned dict)."""
    try:
        cutoff = datetime.now(CST) - timedelta(days=PRUNE_DAYS)
        kept = {}
        for key, rec in records.items():
            try:
                seen = datetime.strptime(rec.get("last_seen", ""),
                                         "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            except Exception:
                seen = None
            if seen is None or seen >= cutoff:
                kept[key] = rec
        availability_file(cfg).write_text(
            json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        return kept
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log.warning("availability persist failed: %s", e)
        return records


def clear_availability(cfg) -> int:
    """Delete all records. Returns how many were cleared."""
    records = load_availability(cfg)
    try:
        availability_file(cfg).unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        log.warning("availability clear failed: %s", e)
    return len(records)


def omitted_nodes(records: dict) -> list[dict]:
    """Currently omitted nodes, for the admin page: name, consecutive
    failures and last-seen time. Sorted by failures desc, then name."""
    out = [{"name": rec.get("name", key), "fails": rec.get("fails", 0),
            "last_seen": rec.get("last_seen", "")}
           for key, rec in records.items()
           if rec.get("fails", 0) >= FAIL_THRESHOLD]
    return sorted(out, key=lambda x: (-x["fails"], x["name"]))


def update_availability(cfg, records: dict, proxies: list[dict],
                        purity: dict) -> tuple[list[dict], dict]:
    """Update consecutive-failure counters from one purity round and
    drop nodes that reached the threshold.

    ``proxies`` is the list that was actually tested (after
    pre-filter); ``purity`` is the {dedup_key: record} result.
    Returns (kept_proxies, stats) and persists the updated records.
    Nodes that were not attempted (``attempts`` == 0) leave their
    counter untouched.
    """
    now = _now_cst()
    for p in proxies:
        key = dedup_key(p)
        rec = purity.get(key)
        if rec is None:
            continue
        name = str(p.get("name", ""))
        if rec.get("measured"):
            if key in records:
                records[key] = {"fails": 0, "name": name, "last_seen": now}
        elif rec.get("attempts", 0) > 0:
            prev = records.get(key, {"fails": 0, "name": name,
                                     "last_seen": now})
            records[key] = {"fails": prev.get("fails", 0) + 1,
                            "name": name, "last_seen": now}
        # attempts == 0: not attempted this round, counter untouched.
    records = save_availability(cfg, records)
    kept = [p for p in proxies
            if records.get(dedup_key(p), {}).get("fails", 0) < FAIL_THRESHOLD]
    stats = {"availability_tracked": len(records),
             "availability_omitted": len(proxies) - len(kept)}
    return kept, stats
