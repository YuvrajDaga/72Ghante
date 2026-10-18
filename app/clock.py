"""One clock for the whole system (agent and mock), with a demo "time travel" offset.

Time travel lets the demo jump to hour 61 of 72 without waiting. It shifts every
reading of now() by the same amount, so deadlines stay consistent.
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_offset = timedelta(0)


def now() -> datetime:
    return datetime.now(IST) + _offset


def advance(hours: float) -> None:
    global _offset
    _offset += timedelta(hours=hours)


def reset() -> None:
    global _offset
    _offset = timedelta(0)


def offset_hours() -> float:
    return _offset.total_seconds() / 3600


def parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)
