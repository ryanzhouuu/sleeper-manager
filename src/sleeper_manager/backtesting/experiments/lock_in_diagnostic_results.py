"""Construct legal automatic assignments and diagnostic model results."""

from __future__ import annotations

from math import isfinite

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
    _candidate_id,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    AutomaticSlotAssignment,
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import TeamWeekReplayResult
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment

_SCORE_BOOST = 1_000_000.0


def legal_automatic_assignments(
    state: ReplayState,
    team_week: HistoricalTeamWeekInput,
) -> tuple[AutomaticSlotAssignment, ...]:
    """Assign every unlocked observed starter to its legal final slot."""

    locked_players = {slot.player_id for slot in state.locked_slots}
    unlocked_starters = tuple(
        player_id
        for player_id in team_week.observed_starter_ids
        if player_id is not None and player_id not in locked_players
    )
    open_indices = state.open_slot_indices
    if len(unlocked_starters) != len(open_indices):
        raise LockInDiagnosticError(
            "Automatic finalization requires one unlocked observed starter per open slot"
        )
    game_by_id = {game.game_id: game for game in state.games}
    candidates: list[AssignmentCandidate] = []
    for player_id in unlocked_starters:
        player_games = tuple(
            item
            for item in state.player_games
            if item.sleeper_id == player_id and item.rostered_at_tipoff
        )
        if not player_games:
            raise LockInDiagnosticError(
                f"Unlocked observed starter {player_id!r} has no rostered games"
            )
        final_game = max(
            player_games,
            key=lambda item: (game_by_id[item.game_id].start_time, item.game_id),
        )
        candidates.append(
            AssignmentCandidate(
                candidate_id=_candidate_id(final_game),
                player_id=final_game.sleeper_id,
                score=final_game.actual_score + _SCORE_BOOST,
                eligible_positions=final_game.eligible_positions,
                game_id=final_game.game_id,
            )
        )
    open_positions = tuple(state.starter_slots[index] for index in open_indices)
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
    locked_indices = {slot.slot_index for slot in state.locked_slots}
    automatic_indices = {item.slot_index for item in assignments}
    if locked_indices & automatic_indices:
        raise LockInDiagnosticError("Automatic assignments collide with locked slots")
    if locked_indices | automatic_indices != set(range(len(state.starter_slots))):
        raise LockInDiagnosticError(
            "Locked plus automatic assignments must cover every starter slot exactly once"
        )
    return tuple(sorted(assignments, key=lambda item: item.slot_index))


def build_model_result(
    request: LockInDiagnosticRequest,
    adapter: DiagnosticPolicyAdapter,
    automatic: tuple[AutomaticSlotAssignment, ...],
) -> TeamWeekReplayResult:
    """Combine locked and automatic scores into the diagnostic model result."""

    locked_score = sum(slot.accepted_fantasy_score for slot in adapter.state.locked_slots)
    automatic_score = sum(item.score for item in automatic)
    if not isfinite(locked_score + automatic_score):
        raise LockInDiagnosticError("Model realized score must be finite")
    decisions = tuple(trace.decision for trace in adapter.policy_traces)
    return TeamWeekReplayResult(
        league_id=request.team_week.league_id,
        week=request.team_week.week,
        roster_id=request.team_week.roster_id,
        policy_name=request.policy_name,
        realized_score=round(locked_score + automatic_score, 6),
        decisions=decisions,
        locked_slots=adapter.state.locked_slots,
        automatic_final_scores=tuple((item.sleeper_id, item.score) for item in automatic),
        eligibility_quality=request.team_week.eligibility_quality.value,
        data_quality="complete" if request.team_week.complete else "partial",
        exclusions=tuple(item.reason.value for item in request.team_week.exclusions),
    )


__all__ = ("build_model_result", "legal_automatic_assignments")
