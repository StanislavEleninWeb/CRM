"""Time helpers: events are UTC timestamps, assessments are plain dates."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_in(timezone: str, *, now: datetime | None = None) -> date:
    """The calendar date in a tenant's time zone."""
    return (now or utcnow()).astimezone(ZoneInfo(timezone)).date()
