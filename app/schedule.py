"""Daily start windows in Beijing time, including the entire end minute."""
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_cst() -> datetime:
    return datetime.now(CST)


def next_allowed(cfg, candidate: datetime) -> datetime:
    candidate = candidate.astimezone(CST)
    start = cfg.data.get("execution_start", "08:00")
    end = cfg.data.get("execution_end", "23:59")
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    minute = candidate.hour * 60 + candidate.minute
    first, last = sh * 60 + sm, eh * 60 + em
    allowed = first <= minute <= last if first <= last else minute >= first or minute <= last
    if allowed:
        return candidate
    opening = candidate.replace(hour=sh, minute=sm, second=0, microsecond=0)
    return opening if opening > candidate else opening + timedelta(days=1)


def in_execution_window(cfg, now: datetime | None = None) -> bool:
    now = now if now is not None else now_cst()
    return next_allowed(cfg, now) == now


def next_execution(cfg, now: datetime | None = None) -> str:
    now = now if now is not None else now_cst()
    candidate = (now + timedelta(seconds=cfg.interval_seconds)
                 if in_execution_window(cfg, now) else now)
    return next_allowed(cfg, candidate).strftime(TIME_FORMAT)


def normalize_next_run(cfg, value: str | None) -> str:
    now = now_cst()
    try:
        candidate = datetime.strptime(value, TIME_FORMAT).replace(tzinfo=CST)
    except (ValueError, TypeError):
        return next_execution(cfg, now)
    return next_allowed(cfg, max(now, candidate)).strftime(TIME_FORMAT)
