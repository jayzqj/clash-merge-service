"""Connectivity failure history for display; output requires a pass in this round."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.merge import dedup_key

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
FAIL_THRESHOLD = 1
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
        path = availability_file(cfg)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
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


def update_availability(cfg, records: dict, proxies: list[dict], purity: dict) -> tuple[list[dict], dict]:
    """Track connectivity only. Every current failure is omitted immediately."""
    now = _now_cst()
    for p in proxies:
        key = dedup_key(p)
        rec = purity.get(key, {})
        if rec.get("connected") is True:
            records.pop(key, None)
        else:
            previous = records.get(key, {})
            records[key] = {"fails": previous.get("fails", 0) + 1,
                            "name": str(p.get("name", "")), "last_seen": now}
    records = save_availability(cfg, records)
    kept = [p for p in proxies if purity.get(dedup_key(p), {}).get("connected") is True]
    return kept, {"availability_tracked": len(records), "availability_omitted": len(proxies) - len(kept),
                  "availability_anomaly_skipped": False}
