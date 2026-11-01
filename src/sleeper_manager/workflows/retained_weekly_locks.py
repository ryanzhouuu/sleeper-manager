"""Retain accepted weekly scoring slots separately from current Sleeper ownership."""

from datetime import datetime
from typing import TYPE_CHECKING

from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    GameOpportunity,
    PlanningGameStatus,
    PlanningReasonCode,
    SourceLineage,
)

if TYPE_CHECKING:
    from sleeper_manager.workflows.planning_inputs import LivePlanningInputs


def retained_weekly_locks(
    inputs: "LivePlanningInputs", *, decision_time: datetime
) -> tuple[GameOpportunity, ...]:
    """Restore only a departed player's confirmed, historical score in an empty fixed slot."""
    profile = inputs.league_profile
    roster = next(
        (row for row in profile.rosters if row.roster_id == profile.manager_roster_id), None
    )
    if roster is None or not inputs.week_window.contains(decision_time):
        return ()
    slots = sorted(
        (slot for slot in profile.roster_slots if slot.is_starting), key=lambda s: s.index
    )
    if len(slots) != len(roster.starter_ids):
        return ()
    empty = {
        slot.index: slot.position
        for slot, player in zip(slots, roster.starter_ids, strict=True)
        if player in (None, "", "0")
    }
    history = {
        (row.player_id, row.game_id): row
        for row in inputs.player_game_evidence
        if row.roster_id == profile.manager_roster_id
    }
    result = []
    restored_players: set[str] = set()
    for lock in inputs.acknowledgements:
        row = history.get((lock.player_id, lock.game_id))
        if (
            lock.action is not AcknowledgedAction.LOCK
            or not lock.reconciled
            or lock.decided_at > decision_time
            or lock.player_id in roster.player_ids
            or lock.player_id in restored_players
            or lock.slot_index is None
            or lock.slot_position is None
            or lock.slot_position != empty.get(lock.slot_index)
            or lock.accepted_fantasy_score is None
            or row is None
            or row.status is not LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
            or row.observed_at > decision_time
            or row.rostered_at_tipoff is not True
            or row.stable_score != lock.accepted_fantasy_score
            or row.scheduled_start is None
            or not inputs.week_window.contains(row.scheduled_start)
            or row.scheduled_start >= lock.decided_at
            or not eligible_for_slot(row.eligible_positions, lock.slot_position)
        ):
            continue
        result.append(
            GameOpportunity(
                sleeper_player_id=lock.player_id,
                provider_player_id=row.provider_player_id,
                game_id=lock.game_id,
                scheduled_start=row.scheduled_start,
                status=PlanningGameStatus.FINAL,
                roster_id=row.roster_id,
                membership_segment=None,
                eligible_slot_indices=(lock.slot_index,),
                eligible_positions=row.eligible_positions,
                rostered_at_tipoff=True,
                availability_status="unknown",
                availability_evidence_at=None,
                projection=None,
                missing_projection_reason=PlanningReasonCode.MISSING_PROJECTION,
                completed_fantasy_score=lock.accepted_fantasy_score,
                finalized_at=lock.decided_at,
                source_lineage=(
                    SourceLineage(
                        source=lock.provenance,
                        version=lock.decision_id,
                        available_as_of=lock.decided_at,
                        retrieved_at=max(row.observed_at, lock.decided_at),
                    ),
                ),
                data_quality="retained-confirmed-lock",
            )
        )
        restored_players.add(lock.player_id)
        empty.pop(lock.slot_index)
    return tuple(result)
