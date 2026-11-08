"""Owner answers for unverified tipoffs, and clearing reports Sleeper does not confirm."""

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

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
    actual_slot: tuple[int, str] | None = None,
) -> LockInOpportunityRecord:
    """Clear a stale report, or apply the owner's answer for an unverified tipoff.

    Locks and passes are never reported here: Sleeper's lock state is the only source.
    A correction retains observed scores and never restores forfeited eligibility.
    """
    del actual_slot
    if now >= current.fantasy_week_end:
        raise ValueError("This fantasy week has ended.")
    if action in {ManagerAction.NOT_LOCKABLE, ManagerAction.LOCKABLE}:
        return _settle_tipoff(current, action, now)
    if action is not ManagerAction.NOT_LOCKED:
        raise ValueError("Locks and passes are read from Sleeper.")
    status = LockInOpportunityStatus.FINALIZING
    if current.status is LockInOpportunityStatus.INELIGIBLE:
        status = current.status
    elif now >= current.action_deadline:
        status = LockInOpportunityStatus.EXPIRED
    elif current.scheduled_start > now:
        status = LockInOpportunityStatus.SCHEDULED
    return replace(
        current,
        status=status,
        acknowledged_action=None,
        acknowledged_at=None,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
    )


def _settle_tipoff(
    current: LockInOpportunityRecord, action: ManagerAction, now: datetime
) -> LockInOpportunityRecord:
    """The owner can see Sleeper's lock button; their answer replaces the inferred tipoff state."""
    if current.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED:
        raise ValueError("Sleeper already shows this game locked.")
    if not current.tipoff_unverified:
        raise ValueError("Only an unverified game can be settled.")
    if current.scheduled_start >= now:
        raise ValueError("This game has not started.")
    if now >= current.action_deadline:
        raise ValueError("This game's action deadline has passed.")
    lockable = action is ManagerAction.LOCKABLE
    status = (
        LockInOpportunityStatus.INELIGIBLE
        if not lockable
        else LockInOpportunityStatus.ACTIVE
        if current.status is LockInOpportunityStatus.INELIGIBLE
        else current.status
    )
    return replace(
        current,
        status=status,
        rostered_at_tipoff=lockable,
        tipoff_unverified=False,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
        trace_json=json.dumps(
            {**json.loads(current.trace_json), "eligibility_forfeited": not lockable}
        ),
    )
