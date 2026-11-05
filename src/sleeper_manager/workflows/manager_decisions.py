"""Validate manager choices and reported locks without treating advice as execution."""

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


class ManagerAction(StrEnum):
    NOT_LOCKED = "not_locked"
    LOCKED = "locked"
    PASSED = "passed"
    NOT_LOCKABLE = "not_lockable"
    LOCKABLE = "lockable"


@dataclass(frozen=True)
class ManagerEdit:
    current: LockInOpportunityRecord
    action: ManagerAction
    actual_slot: tuple[int, str] | None


def reported_decision(
    current: LockInOpportunityRecord,
    action: ManagerAction,
    now: datetime,
    *,
    actual_slot: tuple[int, str] | None,
) -> LockInOpportunityRecord:
    """A correction retains observed scores and never restores forfeited eligibility."""
    if now >= current.fantasy_week_end:
        raise ValueError("This fantasy week has ended.")
    if action is ManagerAction.NOT_LOCKABLE:
        return _mark_not_lockable(current, now)
    if action is ManagerAction.LOCKABLE:
        return _mark_lockable(current, now)
    status = LockInOpportunityStatus.FINALIZING
    slot_index, slot_position = current.slot_index, current.slot_position
    if action is ManagerAction.NOT_LOCKED:
        if current.status is LockInOpportunityStatus.INELIGIBLE:
            status = current.status
        elif now >= current.action_deadline:
            status = LockInOpportunityStatus.EXPIRED
        elif current.scheduled_start > now:
            status = LockInOpportunityStatus.SCHEDULED
    else:
        if current.status in {
            LockInOpportunityStatus.INELIGIBLE,
            LockInOpportunityStatus.EXPIRED,
            LockInOpportunityStatus.RECONCILIATION_REQUIRED,
        }:
            raise ValueError("This performance is not eligible to be locked or passed.")
        if current.scheduled_start >= now or current.stable_score is None:
            raise ValueError("A stable completed game score is required.")
        if now >= current.action_deadline:
            raise ValueError("This game's action deadline has passed.")
        if current.rostered_at_tipoff is not True:
            raise ValueError("This performance is not verified as eligible.")
        if action is ManagerAction.LOCKED:
            if actual_slot is None or not eligible_for_slot(
                current.eligible_positions, actual_slot[1]
            ):
                raise ValueError("The player must occupy an eligible starter slot in Sleeper.")
            slot_index, slot_position = actual_slot
            status = LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        else:
            status = LockInOpportunityStatus.ACKNOWLEDGED_PASSED
    return replace(
        current,
        status=status,
        slot_index=slot_index,
        slot_position=slot_position,
        acknowledged_action=None if action is ManagerAction.NOT_LOCKED else action.value,
        acknowledged_at=None if action is ManagerAction.NOT_LOCKED else now,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
        tipoff_unverified=current.tipoff_unverified and action is not ManagerAction.LOCKED,
    )


def _mark_not_lockable(current: LockInOpportunityRecord, now: datetime) -> LockInOpportunityRecord:
    """Sleeper's answer settles an unverified game; only the owner's override can reopen it."""
    if not current.tipoff_unverified:
        raise ValueError("Only an unverified game can be marked not lockable.")
    return replace(
        current,
        status=LockInOpportunityStatus.INELIGIBLE,
        rostered_at_tipoff=False,
        tipoff_unverified=False,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
        trace_json=_with_forfeited(current, True),
    )


def _mark_lockable(current: LockInOpportunityRecord, now: datetime) -> LockInOpportunityRecord:
    """The owner can see Sleeper's lock button, so this reverses a mark the system inferred."""
    if current.status is not LockInOpportunityStatus.INELIGIBLE:
        raise ValueError("Only an ineligible game can be reopened.")
    if current.scheduled_start >= now:
        raise ValueError("This game has not started.")
    if now >= current.action_deadline:
        raise ValueError("This game's action deadline has passed.")
    return replace(
        current,
        status=LockInOpportunityStatus.ACTIVE,
        rostered_at_tipoff=True,
        tipoff_unverified=True,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
        trace_json=_with_forfeited(current, False),
    )


def _with_forfeited(current: LockInOpportunityRecord, forfeited: bool) -> str:
    return json.dumps({**json.loads(current.trace_json), "eligibility_forfeited": forfeited})
