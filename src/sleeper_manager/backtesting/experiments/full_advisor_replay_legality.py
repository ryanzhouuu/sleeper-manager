"""Lineup matching helpers for full-advisor replay transitions."""

from __future__ import annotations

from collections.abc import Mapping

from sleeper_manager.backtesting.experiments.full_advisor_replay_models import (
    FullAdvisorReplayError,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment


def realign_for_lock(
    *,
    player_id: str,
    target_slot: int,
    lineup: Mapping[int, str],
    active: Mapping[str, int],
    replay_state: ReplayState,
    team_week: HistoricalTeamWeekInput,
) -> dict[int, str]:
    """Place a completed starter while preserving active and previously fixed slots."""

    players = tuple(sorted(set(lineup.values())))
    positions = _positions_by_player(replay_state, team_week)
    candidates = tuple(
        AssignmentCandidate(f"lineup:{item}", item, 1.0, positions[item]) for item in players
    )
    required = {(target_slot, f"lineup:{player_id}")}
    required.update(
        (fixed.slot_index, f"lineup:{fixed.player_id}") for fixed in replay_state.locked_slots
    )
    required.update((slot, f"lineup:{item}") for item, slot in active.items())
    try:
        assignment = maximum_weight_assignment(
            candidates,
            team_week.starter_slots,
            required_edges=frozenset(required),
        )
    except ValueError as error:
        raise FullAdvisorReplayError(f"lock_lineup_infeasible:{error}") from error
    return {
        item.slot_index: item.player_id
        for item in assignment.assignments
        if item.player_id is not None
    }


def automatic_final_scores(
    *,
    team_week: HistoricalTeamWeekInput,
    replay_state: ReplayState,
    started_keys: frozenset[tuple[str, str]],
) -> tuple[tuple[str, float], ...]:
    """Fill every open slot with a final game that the simulated lineup started."""

    game_starts = {game.game_id: game.start_time for game in replay_state.games}
    locked_players = {slot.player_id for slot in replay_state.locked_slots}
    candidates = []
    for player_id in team_week.roster_player_ids:
        if player_id in locked_players:
            continue
        games = tuple(item for item in replay_state.player_games if item.sleeper_id == player_id)
        if not games:
            continue
        final = max(games, key=lambda item: (game_starts[item.game_id], item.game_id))
        if (player_id, final.game_id) not in started_keys:
            continue
        candidates.append(
            AssignmentCandidate(
                f"automatic:{player_id}:{final.game_id}",
                player_id,
                final.actual_score,
                final.eligible_positions,
                final.game_id,
            )
        )
    locked_indices = {slot.slot_index for slot in replay_state.locked_slots}
    open_indices = tuple(
        index for index in range(len(team_week.starter_slots)) if index not in locked_indices
    )
    slots = tuple(team_week.starter_slots[index] for index in open_indices)
    try:
        assigned = maximum_weight_assignment(
            tuple(candidates),
            slots,
            slot_indices=open_indices,
            require_full_cardinality=True,
        )
    except ValueError as error:
        raise FullAdvisorReplayError(f"automatic_assignment_infeasible:{error}") from error
    return tuple(
        (item.player_id, item.score) for item in assigned.assignments if item.player_id is not None
    )


def _positions_by_player(
    replay_state: ReplayState,
    team_week: HistoricalTeamWeekInput,
) -> dict[str, tuple[str, ...]]:
    """Collect stable current eligibility across player-game records."""

    return {
        player_id: tuple(
            sorted(
                {
                    position
                    for item in replay_state.player_games
                    if item.sleeper_id == player_id
                    for position in item.eligible_positions
                }
            )
        )
        for player_id in team_week.roster_player_ids
    }


__all__ = ("automatic_final_scores", "realign_for_lock")
