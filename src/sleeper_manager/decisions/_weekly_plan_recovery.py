"""Verify combined extra attempts by proving an executable return to the same baseline."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.lineup import SlotAssignment
from sleeper_manager.domain.planning import (
    GameOpportunity,
    ObservedStarter,
    PlannedAssignment,
    PlanningGameStatus,
    TeamWeekState,
)
from sleeper_manager.domain.planning_actions import RecoveryObligation


def prove_recovery(
    state: TeamWeekState,
    baseline: tuple[SlotAssignment, ...],
    candidate: tuple[SlotAssignment, ...],
    batch: tuple[GameOpportunity, ...],
    *,
    lead_time: timedelta = timedelta(minutes=10),
) -> RecoveryObligation | None:
    """Allow only a strict additional chance whose joint discard preserves baseline choices."""
    base_players = {item.player_id for item in baseline}
    candidate_players = {item.player_id for item in candidate}
    base_games = {
        (game.sleeper_player_id, game.game_id)
        for game in batch
        if game.sleeper_player_id in base_players
    }
    candidate_games = {
        (game.sleeper_player_id, game.game_id)
        for game in batch
        if game.sleeper_player_id in candidate_players
    }
    added = tuple(sorted(candidate_games - base_games))
    if not added or not base_games < candidate_games:
        return None
    added_players = {player for player, _ in added}
    displaced = base_players - candidate_players - {None}
    passed = {(row.player_id, row.game_id) for row in state.passed_opportunities}
    protected = {
        game.sleeper_player_id
        for game in (*state.active_opportunities, *state.completed_opportunities)
        if (game.sleeper_player_id, game.game_id) not in passed
    }
    if displaced & protected:
        return None
    affected_slots = {
        row.slot_index
        for row in candidate
        if row.player_id
        != next((old.player_id for old in baseline if old.slot_index == row.slot_index), None)
    }
    starts = [
        game.scheduled_start
        for game in state.opportunities
        if (
            game.sleeper_player_id in displaced | added_players
            or affected_slots.intersection(game.eligible_slot_indices)
        )
        and game.scheduled_start > batch[0].scheduled_start
        and game.status is PlanningGameStatus.SCHEDULED
    ]
    if starts:
        recovery_at = min(starts)
        deadline = recovery_at - lead_time
    elif state.week_ends_at is not None:
        recovery_at = deadline = state.week_ends_at
    else:
        return None
    if deadline <= batch[0].scheduled_start:
        return None
    fixed = tuple(
        PlannedAssignment(row.slot_index, row.slot_position, row.player_id)
        for row in state.fixed_slots
    )
    base_view = tuple(
        sorted(
            (
                *fixed,
                *(
                    PlannedAssignment(row.slot_index, row.slot_position, row.player_id)
                    for row in baseline
                ),
            ),
            key=lambda row: row.slot_index,
        )
    )
    candidate_view = tuple(
        sorted(
            (
                *fixed,
                *(
                    PlannedAssignment(row.slot_index, row.slot_position, row.player_id)
                    for row in candidate
                ),
            ),
            key=lambda row: row.slot_index,
        )
    )
    obligation = RecoveryObligation(
        added,
        base_view,
        candidate_view,
        recovery_at,
        deadline,
        state.decision_time,
        state.input_version,
        bench_capacity_floor=len(state.roster_player_ids)
        - len(state.reserve_player_ids)
        - len(state.observed_starters),
    )
    positions = {row.player_id: row.eligible_positions for row in state.observed_starters}
    positions.update(
        {game.sleeper_player_id: game.eligible_positions for game in state.opportunities}
    )
    simulated = replace(
        state,
        observed_starters=tuple(
            ObservedStarter(row.slot_index, row.player_id, positions[row.player_id])
            for row in candidate_view
            if row.player_id is not None
        ),
        opportunities=tuple(
            replace(game, status=PlanningGameStatus.ACTIVE)
            if game.game_id in {game_id for _, game_id in added}
            else game
            for game in state.opportunities
        ),
        recovery_obligations=(*state.recovery_obligations, obligation),
        recovery_forfeitures=added,
        bench_capacity=state.bench_capacity
        if state.bench_capacity is not None
        else obligation.bench_capacity_floor,
    )
    try:
        moves = plan_moves(simulated, base_view, recovery_at, lead_time)
    except WeeklyPlanError:
        return None
    return replace(
        obligation, recovery_moves=tuple(replace(move, deadline=deadline) for move in moves)
    )


def recovery_bench_capacity(
    state: TeamWeekState, recoveries: tuple[RecoveryObligation, ...]
) -> int | None:
    """Reuse verified vacated space, including the current observed bench population."""
    if state.bench_capacity is not None or not recoveries:
        return state.bench_capacity
    current = (
        len(state.roster_player_ids) - len(state.reserve_player_ids) - len(state.observed_starters)
    )
    return max(current, *(row.bench_capacity_floor or 0 for row in recoveries))
