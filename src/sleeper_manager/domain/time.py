from datetime import datetime
from zoneinfo import ZoneInfo


def in_timezone(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(ZoneInfo(timezone_name))
