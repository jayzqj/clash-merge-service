"""Last successful scores, separate from the current round's health.

Keys hash the full connection identity, so renames keep history but
changed credentials do not. No proxy credentials are stored here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.merge import dedup_key
from app.names import parse_score

log = logging.getLogger(__name__)


def _key(proxy: dict) -> str:
    return hashlib.sha256(dedup_key(proxy).encode("utf-8")).hexdigest()


def _valid_score(score) -> bool:
    return type(score) is int and 0 <= score <= 100


def load_history(cfg) -> dict:
    path = cfg.path.parent / "purity_history.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid history")
        return {k: v for k, v in raw.items()
                if isinstance(v, dict) and _valid_score(v.get("score"))}
    except FileNotFoundError:
        # First upgrade: recover scores already present in the published file.
        try:
            doc = yaml.safe_load(Path(cfg.output_path).read_text(encoding="utf-8")) or {}
            return {_key(p): {"score": parse_score(str(p.get("name", "")))}
                    for p in doc.get("proxies", [])
                    if _valid_score(parse_score(str(p.get("name", ""))))}
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            return {}
    except (OSError, ValueError):
        log.warning("purity history unavailable; using current measurements only")
        return {}


def apply_history(cfg, proxies: list[dict], purity: dict) -> dict:
    """Attach cached scores without turning a failed check into a success."""
    history = load_history(cfg)
    now = datetime.now(timezone.utc).isoformat()
    results = {k: dict(v) for k, v in purity.items()}
    for p in proxies:
        key = dedup_key(p)
        rec = results.setdefault(key, {"measured": False, "attempts": 0})
        cached_key = _key(p)
        if rec.get("measured") and _valid_score(rec.get("score")):
            rec["score_source"] = "current"
            history[cached_key] = {"score": rec["score"], "measured_at": now}
        elif cached_key in history:
            rec["score"] = history[cached_key]["score"]
            rec["score_source"] = "history"
        else:
            rec["score"] = None
    path = cfg.path.parent / "purity_history.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("could not persist purity history")
    return results


def filter_by_purity(cfg, proxies: list[dict], purity: dict) -> tuple[list[dict], dict]:
    enabled = bool(cfg.data.get("purity_filter_enabled", False))
    threshold = cfg.data.get("purity_threshold", 15)
    kept = proxies
    if enabled:
        kept = [p for p in proxies
                if _valid_score((rec := purity.get(dedup_key(p), {})).get("score"))
                and rec["score"] <= threshold]
    return kept, {"purity_filter_enabled": enabled,
                  "purity_threshold": threshold,
                  "purity_filter_input": len(proxies),
                  "purity_filter_kept": len(kept),
                  "purity_filter_excluded": len(proxies) - len(kept)}
