"""Validate reported Sleeper actions without treating advice or delivery as execution."""

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
    )
