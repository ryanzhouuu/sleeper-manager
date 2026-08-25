from datetime import UTC, date, datetime, time

import pytest

from sleeper_manager.domain.scheduling import (
    SchedulingError,
    eastern_fantasy_week,
    manager_daily_schedule,
    manager_local_day,
    manager_local_day_window,
)


@pytest.mark.parametrize(
    ("instant", "expected_start", "expected_end"),
    (
        (
            datetime(2026, 1, 7, 18, tzinfo=UTC),
            datetime(2026, 1, 5, 5, tzinfo=UTC),
            datetime(2026, 1, 12, 5, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 8, 18, tzinfo=UTC),
            datetime(2026, 7, 6, 4, tzinfo=UTC),
            datetime(2026, 7, 13, 4, tzinfo=UTC),
        ),
    ),
)
def test_eastern_fantasy_week_tracks_standard_and_daylight_time(
    instant: datetime,
    expected_start: datetime,
    expected_end: datetime,
) -> None:
    window = eastern_fantasy_week(instant)

    assert window.starts_at == expected_start
    assert window.ends_at == expected_end
    assert window.contains(instant)
    assert not window.contains(expected_end)


@pytest.mark.parametrize(
    ("local_day", "expected"),
    (
        (date(2026, 1, 7), datetime(2026, 1, 7, 13, tzinfo=UTC)),
        (date(2026, 7, 7), datetime(2026, 7, 7, 12, tzinfo=UTC)),
    ),
)
def test_manager_daily_schedule_keeps_seven_am_central(local_day: date, expected: datetime) -> None:
    assert manager_daily_schedule(local_day, timezone_name="America/Chicago") == expected


def test_manager_local_day_and_bounds_handle_the_spring_dst_day() -> None:
    instant = datetime(2026, 3, 8, 8, tzinfo=UTC)

    assert manager_local_day(instant, timezone_name="America/Chicago") == date(2026, 3, 8)
    window = manager_local_day_window(instant, timezone_name="America/Chicago")
    assert window.starts_at == datetime(2026, 3, 8, 6, tzinfo=UTC)
    assert window.ends_at == datetime(2026, 3, 9, 5, tzinfo=UTC)


def test_manager_schedule_rejects_invalid_timezone_and_aware_wall_time() -> None:
    with pytest.raises(SchedulingError, match="Unknown timezone"):
        manager_daily_schedule(date(2026, 1, 1), timezone_name="Mars/Olympus")
    with pytest.raises(SchedulingError, match="must not include a timezone"):
        manager_daily_schedule(
            date(2026, 1, 1),
            timezone_name="America/Chicago",
            local_time=time(7, tzinfo=UTC),
        )
