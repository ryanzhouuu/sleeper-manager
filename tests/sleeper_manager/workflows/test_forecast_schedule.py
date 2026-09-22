"""Verify daily, pre-game, coalescing, cap, and lateness rules for one wake."""

from datetime import UTC, datetime, time, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome, ForecastCaptureTiming
from sleeper_manager.workflows.forecast_schedule import (
    FORECAST_CAP_ERROR_CODE,
    ForecastPlanStep,
    ForecastSlotKind,
    forecast_receipt_id,
    plan_forecast_capture,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    SOURCE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)

DAILY = time(hour=7)
NOW = datetime(2026, 9, 22, 21, tzinfo=UTC)
NEAR_TIPOFFS = (
    datetime(2026, 9, 22, 18, tzinfo=UTC),
    datetime(2026, 9, 22, 18, 40, tzinfo=UTC),
)
LATER_TIPOFF = datetime(2026, 9, 22, 20, 30, tzinfo=UTC)
COALESCED_AT = datetime(2026, 9, 22, 17, 30, tzinfo=UTC)
SEPARATE_AT = datetime(2026, 9, 22, 20, tzinfo=UTC)
DAILY_AT = datetime(2026, 9, 22, 7, tzinfo=UTC)


def _plan(**overrides: object) -> tuple[object, ...]:
    """Plan one UTC-day wake, overriding only the case under test."""

    arguments: dict[str, object] = {
        "now": NOW,
        "timezone_name": "UTC",
        "daily_plan_time": DAILY,
        "in_season": True,
        "tipoffs": (*NEAR_TIPOFFS, LATER_TIPOFF),
        "receipts": (),
        "source": SOURCE,
    }
    arguments.update(overrides)
    return plan_forecast_capture(**arguments)  # type: ignore[arg-type]


def test_preseason_schedules_only_the_daily_slot() -> None:
    """Ignore opponent and roster tipoffs before the season starts."""

    before = _plan(
        now=DAILY_AT - timedelta(minutes=1),
        in_season=False,
    )
    due = _plan(in_season=False)

    assert before == ()
    assert len(due) == 1
    assert due[0].kind is ForecastSlotKind.DAILY
    assert due[0].step is ForecastPlanStep.FETCH
    assert due[0].scheduled_for == DAILY_AT
    assert due[0].receipt_id == forecast_receipt_id(SOURCE, DAILY_AT)


def test_in_season_coalesces_targets_within_one_hour_and_prefers_pregame() -> None:
    """Fetch the earliest pre-game slot before the already-due daily slot."""

    actions = _plan()

    assert len(actions) == 1
    assert actions[0].step is ForecastPlanStep.FETCH
    assert actions[0].kind is ForecastSlotKind.PRE_TIPOFF
    assert actions[0].scheduled_for == COALESCED_AT
    assert actions[0].timing is ForecastCaptureTiming.LATE


def test_fetch_within_one_cron_interval_is_on_time() -> None:
    """Keep a wake that lands on the five-minute boundary on time."""

    on_time = _plan(now=COALESCED_AT + timedelta(minutes=5))
    late = _plan(now=COALESCED_AT + timedelta(minutes=6))

    assert on_time[0].scheduled_for == COALESCED_AT
    assert on_time[0].timing is ForecastCaptureTiming.ON_TIME
    assert late[0].timing is ForecastCaptureTiming.LATE


def test_completed_slot_is_not_repeated_even_from_an_earlier_utc_day() -> None:
    """Treat any stored receipt for the slot as terminal."""

    receipt = successful_capture(
        receipt_id=forecast_receipt_id(SOURCE, COALESCED_AT),
        persisted_at=datetime(2026, 9, 21, 23, tzinfo=UTC),
    ).receipt
    actions = _plan(
        now=COALESCED_AT,
        tipoffs=NEAR_TIPOFFS,
        receipts=(receipt,),
        daily_plan_time=time(hour=22),
    )

    assert actions == ()


def test_cap_prefers_a_pregame_fetch_then_suppresses_the_rest() -> None:
    """Spend the last request on a pre-game slot and record daily-cap gaps after that."""

    used = tuple(
        failed_capture(
            receipt_id=f"request-{index}",
            persisted_at=datetime(2026, 9, 22, 1, index, tzinfo=UTC),
        ).receipt
        for index in range(4)
    )
    fetch = _plan(receipts=used)
    assert len(fetch) == 1
    assert fetch[0].step is ForecastPlanStep.FETCH
    assert fetch[0].kind is ForecastSlotKind.PRE_TIPOFF

    capped = (
        *used,
        failed_capture(
            receipt_id="request-4",
            persisted_at=datetime(2026, 9, 22, 3, tzinfo=UTC),
        ).receipt,
    )
    suppressed = _plan(receipts=capped)

    assert {action.step for action in suppressed} == {ForecastPlanStep.SUPPRESS}
    assert {action.scheduled_for for action in suppressed} == {DAILY_AT, COALESCED_AT, SEPARATE_AT}
    assert {action.error_code for action in suppressed} == {FORECAST_CAP_ERROR_CODE}


def test_suppressed_receipts_do_not_consume_request_cap() -> None:
    """Count changed, unchanged, failed, and invalid attempts, not gaps."""

    gaps = tuple(
        suppressed_capture(
            receipt_id=f"gap-{index}",
            persisted_at=datetime(2026, 9, 22, 2, index, tzinfo=UTC),
        ).receipt
        for index in range(5)
    )
    unchanged = successful_capture(
        receipt_id="unchanged-other",
        persisted_at=datetime(2026, 9, 22, 4, tzinfo=UTC),
        outcome=ForecastCaptureOutcome.UNCHANGED,
    ).receipt

    actions = _plan(receipts=(*gaps, unchanged))

    assert len(actions) == 1
    assert actions[0].step is ForecastPlanStep.FETCH


def test_naive_planning_time_is_rejected() -> None:
    """Refuse a clock that cannot be ordered against stored receipts."""

    with pytest.raises(ValueError, match="timezone-aware"):
        _plan(now=datetime(2026, 9, 22, 21))
