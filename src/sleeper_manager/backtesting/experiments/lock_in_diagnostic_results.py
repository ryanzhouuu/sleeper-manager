"""Construct legal automatic assignments and diagnostic model results."""

from __future__ import annotations

from math import isfinite

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    AutomaticSlotAssignment,
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.replay.models import TeamWeekReplayResult
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment
from sleeper_manager.domain.lock_in import LockInDecisionTrace
from sleeper_manager.domain.planning import FixedSlot, TeamWeekState

_SCORE_BOOST = 1_000_000.0


def legal_automatic_assignments(state: TeamWeekState) -> tuple[AutomaticSlotAssignment, ...]:
    """Assign every unlocked observed starter to its legal final slot."""

    locked_players = {slot.player_id for slot in state.fixed_slots}
    unlocked_starters = tuple(
        observed.player_id
        for observed in state.observed_starters
        if observed.player_id not in locked_players
    )
    open_indices = state.open_slot_indices
    if len(unlocked_starters) != len(open_indices):
        raise LockInDiagnosticError(
            "Automatic finalization requires one unlocked observed starter per open slot"
        )
    slot_by_index = {slot.index: slot for slot in state.starter_slots}
    candidates: list[AssignmentCandidate] = []
    for player_id in unlocked_starters:
        player_opportunities = tuple(
            opportunity
            for opportunity in state.opportunities
            if opportunity.sleeper_player_id == player_id
            if opportunity.rostered_at_tipoff is True
            if opportunity.completed_fantasy_score is not None
        )
        if not player_opportunities:
            raise LockInDiagnosticError(
                f"Unlocked observed starter {player_id!r} has no rostered scored games"
            )
        final_opportunity = max(
            player_opportunities,
            key=lambda item: (item.scheduled_start, item.game_id),
        )
        score = final_opportunity.completed_fantasy_score
        assert score is not None
        candidates.append(
            AssignmentCandidate(
                candidate_id=(f"{final_opportunity.sleeper_player_id}:{final_opportunity.game_id}"),
                player_id=final_opportunity.sleeper_player_id,
                score=score + _SCORE_BOOST,
                eligible_positions=final_opportunity.eligible_positions,
                game_id=final_opportunity.game_id,
                eligible_slot_indices=final_opportunity.eligible_slot_indices,
            )
        )
    open_positions = tuple(slot_by_index[index].position for index in open_indices)
    try:
        result = maximum_weight_assignment(
            tuple(candidates),
            open_positions,
            slot_indices=open_indices,
        )
    except ValueError as error:
        raise LockInDiagnosticError(
            f"Automatic final assignment is positionally infeasible: {error}"
        ) from error
    assignments: list[AutomaticSlotAssignment] = []
    assigned_players: set[str] = set()
    for item in result.assignments:
        if item.player_id is None or item.game_id is None:
            raise LockInDiagnosticError(
                "Automatic finalization left an open slot empty; remainder is infeasible"
            )
        if item.player_id in assigned_players:
            raise LockInDiagnosticError("Automatic finalization reused a player")
        assigned_players.add(item.player_id)
        assignments.append(
            AutomaticSlotAssignment(
                slot_index=item.slot_index,
                slot_position=item.slot_position,
                sleeper_id=item.player_id,
                game_id=item.game_id,
                score=round(item.score - _SCORE_BOOST, 6),
            )
        )
    if assigned_players != set(unlocked_starters):
        raise LockInDiagnosticError(
            "Automatic finalization failed to place every unlocked observed starter"
        )
    locked_indices = {slot.slot_index for slot in state.fixed_slots}
    automatic_indices = {item.slot_index for item in assignments}
    if locked_indices & automatic_indices:
        raise LockInDiagnosticError("Automatic assignments collide with locked slots")
    starter_indices = {slot.index for slot in state.starter_slots}
    if locked_indices | automatic_indices != starter_indices:
        raise LockInDiagnosticError(
            "Locked plus automatic assignments must cover every starter slot exactly once"
        )
    return tuple(sorted(assignments, key=lambda item: item.slot_index))


def build_model_result(
    request: LockInDiagnosticRequest,
    *,
    locked_slots: tuple[FixedSlot, ...],
    policy_traces: tuple[LockInDecisionTrace, ...],
    automatic: tuple[AutomaticSlotAssignment, ...],
) -> TeamWeekReplayResult:
    """Combine locked and automatic scores into the diagnostic model result."""

    locked_score = sum(slot.accepted_fantasy_score for slot in locked_slots)
    automatic_score = sum(item.score for item in automatic)
    if not isfinite(locked_score + automatic_score):
        raise LockInDiagnosticError("Model realized score must be finite")
    decisions = tuple(trace.decision for trace in policy_traces)
    return TeamWeekReplayResult(
        league_id=request.team_week.league_id,
        week=request.team_week.week,
        roster_id=request.team_week.roster_id,
        policy_name=request.policy_name,
        realized_score=round(locked_score + automatic_score, 6),
        decisions=decisions,
        locked_slots=locked_slots,
        automatic_final_scores=tuple((item.sleeper_id, item.score) for item in automatic),
        eligibility_quality=request.team_week.eligibility_quality.value,
        data_quality="complete" if request.team_week.complete else "partial",
        exclusions=tuple(item.reason.value for item in request.team_week.exclusions),
    )


__all__ = ("build_model_result", "legal_automatic_assignments")
