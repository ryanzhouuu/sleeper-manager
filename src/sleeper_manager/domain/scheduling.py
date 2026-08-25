from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EASTERN_TIMEZONE = "America/New_York"


class SchedulingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TimeWindow:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.starts_at)
        _require_aware(self.ends_at)
        if self.starts_at >= self.ends_at:
            raise SchedulingError("Time-window end must follow its start")

    def contains(self, at: datetime) -> bool:
        _require_aware(at)
        return self.starts_at <= at < self.ends_at


def eastern_fantasy_week(at: datetime) -> TimeWindow:
    """Return the Monday-to-Monday Eastern fantasy week containing ``at``."""
    _require_aware(at)
    eastern = ZoneInfo(EASTERN_TIMEZONE)
    local = at.astimezone(eastern)
    monday = local.date() - timedelta(days=local.weekday())
    starts_at = datetime.combine(monday, time.min, eastern).astimezone(UTC)
    ends_at = datetime.combine(monday + timedelta(days=7), time.min, eastern).astimezone(UTC)
    return TimeWindow(starts_at, ends_at)


def manager_daily_schedule(
    local_day: date,
    *,
    timezone_name: str,
    local_time: time = time(hour=7),
) -> datetime:
    """Resolve a manager-local wall-clock schedule to an exact UTC instant."""
    timezone = _timezone(timezone_name)
    if local_time.tzinfo is not None:
        raise SchedulingError("Manager daily time must not include a timezone")
    return datetime.combine(local_day, local_time, timezone).astimezone(UTC)


def manager_local_day(at: datetime, *, timezone_name: str) -> date:
    _require_aware(at)
    return at.astimezone(_timezone(timezone_name)).date()


def manager_local_day_window(at: datetime, *, timezone_name: str) -> TimeWindow:
    local_day = manager_local_day(at, timezone_name=timezone_name)
    timezone = _timezone(timezone_name)
    starts_at = datetime.combine(local_day, time.min, timezone).astimezone(UTC)
    ends_at = datetime.combine(local_day + timedelta(days=1), time.min, timezone).astimezone(UTC)
    return TimeWindow(starts_at, ends_at)


def _timezone(name: str) -> ZoneInfo:
    if not name.strip():
        raise SchedulingError("Timezone name must be non-empty")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise SchedulingError(f"Unknown timezone: {name!r}") from error


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulingError("Scheduling timestamps must be timezone-aware")


__all__ = (
    "EASTERN_TIMEZONE",
    "SchedulingError",
    "TimeWindow",
    "eastern_fantasy_week",
    "manager_daily_schedule",
    "manager_local_day",
    "manager_local_day_window",
)
