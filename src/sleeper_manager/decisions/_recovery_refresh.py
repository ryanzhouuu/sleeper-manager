"""Revalidate pending return paths against current lineups, results, and changed tipoffs."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions._weekly_plan_recovery import recovery_bench_capacity
from sleeper_manager.domain.planning import PlanningGameStatus, PlanningReasonCode, TeamWeekState


def refresh_recoveries(state: TeamWeekState, lead_time: timedelta) -> TeamWeekState:
    """Keep data gaps pending; supersede ignored attempts and record verified missed windows."""
    starters = {row.player_id for row in state.observed_starters}
    locked = {row.player_id for row in state.fixed_slots}
    games = {(row.sleeper_player_id, row.game_id): row for row in state.opportunities}
    pending = []
    failures = list(state.recovery_failures)
    for recovery in state.recovery_obligations:
        added = tuple(key for key in recovery.added_player_games if key[0] in starters - locked)
        if not added:
            continue
        if any(key not in games for key in added):
            return replace(
                state,
                blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.AMBIGUOUS_GAME_TIME),
            )
        start = min(games[key].scheduled_start for key in added)
        affected = {
            row.slot_index
            for row in recovery.attempt_assignments
            if row.player_id
            != next(
                (
                    old.player_id
                    for old in recovery.baseline_assignments
                    if old.slot_index == row.slot_index
                ),
                None,
            )
        }
        players = {row.player_id for row in recovery.baseline_assignments if row.player_id}
        related = tuple(
            game
            for game in state.opportunities
            if game.sleeper_player_id not in locked | set(state.reserve_player_ids)
            and (
                (game.sleeper_player_id, game.game_id) in added
                or (
                    game.scheduled_start > start
                    and (
                        game.sleeper_player_id in players
                        or affected.intersection(game.eligible_slot_indices)
                    )
                )
            )
        )
        if any(
            game.status in {PlanningGameStatus.POSTPONED, PlanningGameStatus.UNKNOWN}
            for game in related
        ):
            return replace(
                state,
                blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.AMBIGUOUS_GAME_TIME),
            )
        if (
            all(games[key].status is PlanningGameStatus.SCHEDULED for key in added)
            and start >= recovery.recovery_at
        ):
            continue
        later = tuple(
            game
            for game in related
            if game.scheduled_start > start
            and game.status
            in {PlanningGameStatus.SCHEDULED, PlanningGameStatus.ACTIVE, PlanningGameStatus.FINAL}
        )
        at = min((game.scheduled_start for game in later), default=state.week_ends_at)
        if at is None:
            return replace(
                state,
                blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.AMBIGUOUS_GAME_TIME),
            )
        if state.week_ends_at is not None:
            at = min(at, state.week_ends_at)
        deadline = at if at == state.week_ends_at else at - lead_time
        if deadline <= recovery.created_at:
            return replace(
                state,
                blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.AMBIGUOUS_GAME_TIME),
            )
        if at <= state.decision_time and (
            at == state.week_ends_at
            or any(
                game.scheduled_start == at
                and game.status in {PlanningGameStatus.ACTIVE, PlanningGameStatus.FINAL}
                for game in later
            )
        ):
            failures.append(recovery.recovery_id)
            continue
        refreshed = replace(recovery, added_player_games=added, recovery_at=at, deadline=deadline)
        if not {row.player_id for row in refreshed.baseline_assignments if row.player_id} <= set(
            state.roster_player_ids
        ) - set(state.reserve_player_ids):
            continue
        try:
            proof = replace(
                state,
                recovery_forfeitures=added,
                bench_capacity=recovery_bench_capacity(state, (recovery,)),
            )
            moves = plan_moves(proof, refreshed.baseline_assignments, at, at - deadline)
        except WeeklyPlanError:
            continue
        pending.append(replace(refreshed, recovery_moves=moves))
    warnings = state.warnings
    if failures and PlanningReasonCode.RECOVERY_WINDOW_CLOSED not in warnings:
        warnings = (*warnings, PlanningReasonCode.RECOVERY_WINDOW_CLOSED)
    return replace(
        state,
        recovery_obligations=tuple(pending),
        recovery_failures=tuple(dict.fromkeys(failures)),
        warnings=warnings,
    )


def due_recovery_forfeitures(
    state: TeamWeekState, lead_time: timedelta
) -> tuple[tuple[str, str], ...]:
    """Allow a known decline now, or an unresolved trial only near its recovery deadline."""
    passed = {(row.player_id, row.game_id) for row in state.passed_opportunities}
    locked = {row.player_id for row in state.fixed_slots}
    games = {(row.sleeper_player_id, row.game_id): row for row in state.opportunities}
    return tuple(
        sorted(
            {
                key
                for recovery in state.recovery_obligations
                for key in recovery.added_player_games
                if key in games
                and key[0] not in locked
                and (
                    (games[key].status is PlanningGameStatus.FINAL and key in passed)
                    or (
                        games[key].status in {PlanningGameStatus.ACTIVE, PlanningGameStatus.FINAL}
                        and state.decision_time >= recovery.deadline - lead_time
                    )
                )
            }
        )
    )
