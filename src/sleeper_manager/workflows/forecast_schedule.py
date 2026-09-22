"""Choose at most one forecast fetch for a five-minute wake.

The planner is pure: callers supply the clock, league phase, relevant tipoffs,
and receipts already stored. It does not fetch the feed or decide whether a
forecast is suitable for advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256

from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastFetchReceipt,
    ForecastSource,
)
from sleeper_manager.domain.scheduling import manager_daily_schedule, manager_local_day

FORECAST_DAILY_CAP = 5
FORECAST_PRE_TIPOFF_LEAD = timedelta(minutes=30)
FORECAST_COALESCE_WINDOW = timedelta(hours=1)
FORECAST_ON_TIME_GRACE = timedelta(minutes=5)
FORECAST_CAP_ERROR_CODE = "daily_cap"

_REQUEST_OUTCOMES = frozenset(
    {
        ForecastCaptureOutcome.CHANGED,
        ForecastCaptureOutcome.UNCHANGED,
        ForecastCaptureOutcome.FAILED,
        ForecastCaptureOutcome.INVALID,
    }
)


class ForecastSlotKind(StrEnum):
    """Distinguishes the daily snapshot from a coalesced pre-game snapshot."""

    DAILY = "daily"
    PRE_TIPOFF = "pre_tipoff"


class ForecastPlanStep(StrEnum):
    """Whether a due slot should request the feed or record a gap."""

    FETCH = "fetch"
    SUPPRESS = "suppress"


@dataclass(frozen=True, slots=True)
class ForecastPlanAction:
    """One durable capture decision for a single scheduled slot."""

    scheduled_for: datetime
    kind: ForecastSlotKind
    step: ForecastPlanStep
    timing: ForecastCaptureTiming
    receipt_id: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        """Require a suppression reason and an aware slot time."""

        _require_aware(self.scheduled_for, "Forecast slot")
        if self.step is ForecastPlanStep.SUPPRESS and not self.error_code:
            raise ValueError("Suppressed forecast slots require an error code")
        if self.step is ForecastPlanStep.FETCH and self.error_code is not None:
            raise ValueError("Forecast fetches cannot carry a suppression error")


def forecast_receipt_id(source: ForecastSource, scheduled_for: datetime) -> str:
    """Identify one slot so a crash before persistence retries the same receipt."""

    _require_aware(scheduled_for, "Forecast slot")
    identity = "\n".join(
        (
            source.provider,
            source.endpoint,
            source.season,
            source.season_type,
            source.horizon,
            source.adapter_version,
            scheduled_for.astimezone(UTC).isoformat(),
        )
    )
    return sha256(identity.encode()).hexdigest()


def plan_forecast_capture(
    *,
    now: datetime,
    timezone_name: str,
    daily_plan_time: time,
    in_season: bool,
    tipoffs: tuple[datetime, ...],
    receipts: tuple[ForecastFetchReceipt, ...],
    source: ForecastSource,
) -> tuple[ForecastPlanAction, ...]:
    """Return one fetch, every due suppression, or no action for this wake.

    Receipts persisted on the current UTC day count toward the five-request cap.
    Any supplied receipt completes its slot, including attempts stored earlier.
    Outside the season, tipoffs are ignored and only the daily slot is considered.
    Pre-game slots are chosen ahead of the daily slot when both are due.
    """

    _require_aware(now, "Forecast planning time")
    for tipoff in tipoffs:
        _require_aware(tipoff, "Forecast tipoff")
    due = _due_slots(
        now=now,
        timezone_name=timezone_name,
        daily_plan_time=daily_plan_time,
        in_season=in_season,
        tipoffs=tipoffs,
        completed={receipt.receipt_id for receipt in receipts},
        source=source,
    )
    if not due:
        return ()
    if _requests_used(receipts, now) >= FORECAST_DAILY_CAP:
        return tuple(_suppress(slot, now) for slot in due)
    return (_fetch(due[0], now),)


@dataclass(frozen=True, slots=True)
class _Slot:
    scheduled_for: datetime
    kind: ForecastSlotKind
    receipt_id: str


def _due_slots(
    *,
    now: datetime,
    timezone_name: str,
    daily_plan_time: time,
    in_season: bool,
    tipoffs: tuple[datetime, ...],
    completed: set[str],
    source: ForecastSource,
) -> tuple[_Slot, ...]:
    """Return due, unfinished slots with pre-game captures ahead of the daily one."""

    local_day = manager_local_day(now, timezone_name=timezone_name)
    daily_at = manager_daily_schedule(
        local_day,
        timezone_name=timezone_name,
        local_time=daily_plan_time,
    )
    scheduled = {daily_at: ForecastSlotKind.DAILY}
    if in_season:
        for target in _coalesced_targets(tipoffs):
            scheduled[target] = ForecastSlotKind.PRE_TIPOFF
    slots = tuple(
        _Slot(moment, kind, forecast_receipt_id(source, moment))
        for moment, kind in scheduled.items()
        if moment <= now and forecast_receipt_id(source, moment) not in completed
    )
    return tuple(
        sorted(
            slots,
            key=lambda slot: (slot.kind is not ForecastSlotKind.PRE_TIPOFF, slot.scheduled_for),
        )
    )


def _coalesced_targets(tipoffs: tuple[datetime, ...]) -> tuple[datetime, ...]:
    """Collapse pre-game targets that fall within one hour of their group start."""

    targets = sorted({tipoff - FORECAST_PRE_TIPOFF_LEAD for tipoff in tipoffs})
    if not targets:
        return ()
    groups = [targets[0]]
    for target in targets[1:]:
        if target - groups[-1] <= FORECAST_COALESCE_WINDOW:
            continue
        groups.append(target)
    return tuple(groups)


def _requests_used(receipts: tuple[ForecastFetchReceipt, ...], now: datetime) -> int:
    """Count feed requests persisted during the UTC day containing ``now``."""

    utc_now = now.astimezone(UTC)
    utc_start = datetime.combine(utc_now.date(), time.min, UTC)
    utc_end = utc_start + timedelta(days=1)
    return sum(
        1
        for receipt in receipts
        if receipt.outcome in _REQUEST_OUTCOMES
        and utc_start <= receipt.persisted_at.astimezone(UTC) < utc_end
    )


def _fetch(slot: _Slot, now: datetime) -> ForecastPlanAction:
    """Build the single request allowed on this wake."""

    return ForecastPlanAction(
        scheduled_for=slot.scheduled_for,
        kind=slot.kind,
        step=ForecastPlanStep.FETCH,
        timing=_timing(now, slot.scheduled_for),
        receipt_id=slot.receipt_id,
    )


def _suppress(slot: _Slot, now: datetime) -> ForecastPlanAction:
    """Record a cap gap without requesting the feed."""

    return ForecastPlanAction(
        scheduled_for=slot.scheduled_for,
        kind=slot.kind,
        step=ForecastPlanStep.SUPPRESS,
        timing=_timing(now, slot.scheduled_for),
        receipt_id=slot.receipt_id,
        error_code=FORECAST_CAP_ERROR_CODE,
    )


def _timing(now: datetime, scheduled_for: datetime) -> ForecastCaptureTiming:
    """Treat one cron interval after the target as still on time."""

    if now <= scheduled_for + FORECAST_ON_TIME_GRACE:
        return ForecastCaptureTiming.ON_TIME
    return ForecastCaptureTiming.LATE


def _require_aware(value: datetime, label: str) -> None:
    """Reject a timestamp that cannot be ordered against the schedule."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = (
    "FORECAST_CAP_ERROR_CODE",
    "FORECAST_DAILY_CAP",
    "ForecastPlanAction",
    "ForecastPlanStep",
    "ForecastSlotKind",
    "forecast_receipt_id",
    "plan_forecast_capture",
)
