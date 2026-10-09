"""Time helpers: events are UTC timestamps, assessments are plain dates."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_in(timezone: str, *, now: datetime | None = None) -> date:
    """The calendar date in a tenant's time zone."""
    return (now or utcnow()).astimezone(ZoneInfo(timezone)).date()


def local_to_utc(local: datetime, timezone: str) -> datetime:
    """A wall-clock time in a tenant's zone as an instant.

    A time that does not exist (clocks went forward) is refused. A time that occurs twice
    (clocks went back) means its first occurrence.
    """
    if local.tzinfo is not None:
        raise ValueError("expected a local time without an offset")
    zone = ZoneInfo(timezone)
    instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("that time does not exist on that day: the clocks go forward")
    return instant
