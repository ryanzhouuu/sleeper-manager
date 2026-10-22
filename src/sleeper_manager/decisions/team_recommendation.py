"""Coordinate score acceptance, starter positions, and recoverable attempts from one snapshot."""

from dataclasses import replace
from datetime import datetime, timedelta

from sleeper_manager.decisions._coordinated_continuations import coordinate_completed_continuations
from sleeper_manager.decisions._recovery_refresh import due_recovery_forfeitures, refresh_recoveries
from sleeper_manager.decisions._weekly_plan_inputs import (
    next_actionable_batch,
    protected_performances,
)
from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions._weekly_plan_recovery import recovery_bench_capacity
from sleeper_manager.decisions.completed_scores import (
    compare_completed_scores,
    usable_completed_games,
)
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig, build_weekly_plan
from sleeper_manager.domain.lineup_moves import apply_lineup_moves
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    PlanConfidence,
    PlanningReasonCode,
    PlanStatus,
    TeamWeekState,
    WeeklyPlan,
)
from sleeper_manager.domain.planning_actions import PlannedScoreAction


def score_deadline(
    state: TeamWeekState, game: GameOpportunity, lead_time: timedelta
) -> datetime | None:
    """Cap optional acceptance at the next player tipoff or the known week end."""
    later = [
        row.scheduled_start - lead_time
        for row in state.opportunities
        if row.sleeper_player_id == game.sleeper_player_id
        and row.scheduled_start > game.scheduled_start
    ]
    if state.week_ends_at is not None:
        later.append(state.week_ends_at)
    return min(later) if later else None


def build_team_recommendation(
    state: TeamWeekState,
    *,
    lead_time: timedelta = timedelta(minutes=10),
    policy: WeeklyPlanPolicyConfig | None = None,
) -> WeeklyPlan:
    """Return one ordered conditional plan; proposed actions never mutate confirmed state."""
    config = policy or WeeklyPlanPolicyConfig()
    state = refresh_recoveries(state, lead_time)
    if state.is_blocked:
        return _with_roster(build_weekly_plan(state, lead_time=lead_time, policy=config), state)
    completed = usable_completed_games(state)
    try:
        comparison = (
            compare_completed_scores(
                state,
                scenario_count=config.postgame_scenario_count,
                seed=config.seed,
                tie_tolerance=config.tie_tolerance,
                minimum_confidence=0,
            )
            if completed
            else None
        )
        if comparison is not None:
            comparison = coordinate_completed_continuations(state, comparison, config, lead_time)
    except WeeklyPlanError as error:
        blocked = replace(
            state,
            blocking_reasons=(
                PlanningReasonCode.SEARCH_LIMIT
                if "limit" in str(error).lower()
                else PlanningReasonCode.ROSTER_STATE_MISMATCH,
            ),
        )
        return _with_roster(build_weekly_plan(blocked, policy=config), state)
    selected = comparison.selected if comparison else None
    locks = {(row.player_id, row.game_id): row for row in selected.locks} if selected else {}
    actions = []
    for game in completed:
        key = (game.sleeper_player_id, game.game_id)
        slot = locks.get(key)
        deadline = score_deadline(state, game, lead_time)
        if deadline is None:
            blocked = replace(state, blocking_reasons=(PlanningReasonCode.AMBIGUOUS_GAME_TIME,))
            return _with_roster(build_weekly_plan(blocked, policy=config), state)
        if deadline <= state.decision_time:
            blocked = replace(state, blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,))
            return _with_roster(build_weekly_plan(blocked, policy=config), state)
        assert comparison is not None
        opposing = tuple(
            option
            for option in comparison.alternatives
            if any((row.player_id, row.game_id) == key for row in option.locks) != bool(slot)
        )
        alternative = max(opposing, key=lambda option: option.expected_score) if opposing else None
        confidence = (
            sum(
                a > b + config.tie_tolerance
                for a, b in zip(selected.terminal_scores, alternative.terminal_scores, strict=True)
            )
            / len(selected.terminal_scores)
            if alternative is not None and selected is not None
            else 1.0
        )
        if slot is None and (alternative is None or confidence < config.minimum_lock_in_confidence):
            continue
        placement_values: dict[int, float] = {}
        for option in comparison.alternatives:
            for row in option.locks:
                if (row.player_id, row.game_id) == key:
                    placement_values[row.slot_index] = max(
                        placement_values.get(row.slot_index, float("-inf")), option.expected_score
                    )
        actions.append(
            PlannedScoreAction(
                game.sleeper_player_id,
                game.game_id,
                LockInDecisionKind.LOCK if slot else LockInDecisionKind.PASS,
                game.completed_fantasy_score or 0,
                deadline,
                slot.slot_index if slot else None,
                slot.slot_position if slot else None,
                tuple(sorted(placement_values.items())),
                alternative.expected_score if alternative else None,
                confidence,
            )
        )
    virtual_fixed = tuple(
        FixedSlot(
            action.slot_index or 0,
            action.slot_position or "",
            action.player_id,
            action.game_id,
            action.score,
            state.decision_time,
            f"proposed:{action.player_id}:{action.game_id}",
            "proposed-only",
        )
        for action in actions
        if action.kind is LockInDecisionKind.LOCK
    )
    positions = dict(state.roster_eligibility)
    positions.update({row.player_id: row.eligible_positions for row in state.observed_starters})
    for game in state.opportunities:
        positions.setdefault(game.sleeper_player_id, game.eligible_positions)
    observed_recoveries = state.recovery_obligations
    if any(recovery.deadline <= state.decision_time for recovery in observed_recoveries):
        blocked = replace(state, blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,))
        return _with_roster(build_weekly_plan(blocked, policy=config), state)
    conditional = replace(
        state,
        fixed_slots=(*state.fixed_slots, *virtual_fixed),
        observed_starters=tuple(
            ObservedStarter(row.slot_index, row.player_id, positions[row.player_id])
            for row in selected.assignments
            if row.player_id is not None
        )
        if selected
        else state.observed_starters,
        passed_opportunities=(
            *state.passed_opportunities,
            *(
                PassedOpportunity(
                    action.player_id,
                    action.game_id,
                    state.decision_time,
                    f"proposed:{action.player_id}:{action.game_id}",
                    "proposed-only",
                )
                for action in actions
                if action.kind is LockInDecisionKind.PASS
            ),
        ),
    )
    conditional = refresh_recoveries(conditional, lead_time)
    recoveries = conditional.recovery_obligations
    batch = next_actionable_batch(conditional)
    forfeitures = due_recovery_forfeitures(conditional, lead_time)
    conditional = replace(
        conditional,
        recovery_obligations=recoveries,
        recovery_forfeitures=forfeitures,
        bench_capacity=recovery_bench_capacity(state, recoveries),
    )
    plan = build_weekly_plan(
        conditional,
        lead_time=lead_time,
        policy=config,
        _decision=selected.upcoming_decision if selected else None,
    )
    unresolved = {game.sleeper_player_id for game in completed} - {
        action.player_id for action in actions
    }
    if unresolved - {row.player_id for row in plan.desired_assignments}:
        blocked = replace(state, blocking_reasons=(PlanningReasonCode.LOW_CONFIDENCE,))
        return _with_roster(build_weekly_plan(blocked, policy=config), state)
    if batch is None and forfeitures and recoveries:
        recovery = min(recoveries, key=lambda row: row.deadline)
        try:
            moves = plan_moves(
                conditional,
                recovery.baseline_assignments,
                recovery.recovery_at,
                recovery.recovery_at - recovery.deadline,
            )
            plan = replace(
                plan,
                desired_assignments=recovery.baseline_assignments,
                moves=moves,
                status=PlanStatus.ACTION_REQUIRED if moves else PlanStatus.NO_ACTION,
            )
        except WeeklyPlanError:
            plan = build_weekly_plan(
                replace(conditional, blocking_reasons=(PlanningReasonCode.ROSTER_STATE_MISMATCH,)),
                policy=config,
            )
    if plan.status is PlanStatus.BLOCKED:
        return _with_roster(
            build_weekly_plan(
                replace(state, blocking_reasons=plan.blocking_reasons), policy=config
            ),
            state,
        )
    pre_moves = (
        tuple(
            replace(
                move,
                deadline=min(move.deadline - lead_time, *(action.deadline for action in actions)),
            )
            for move in selected.moves
        )
        if selected and actions
        else ()
    )
    if any(move.deadline <= state.decision_time for move in pre_moves):
        blocked = replace(state, blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,))
        return _with_roster(build_weekly_plan(blocked, policy=config), state)
    observed = tuple(
        replace(
            row,
            player_id=next(
                (
                    item.player_id
                    for item in state.observed_starters
                    if item.slot_index == row.slot_index
                ),
                None,
            ),
        )
        for row in plan.observed_assignments
    )
    occupancy = {row.slot_index: row.player_id for row in observed}
    for move in (*pre_moves, *plan.moves):
        occupancy = apply_lineup_moves((move,), occupancy)
        if unresolved - set(occupancy.values()):
            blocked = replace(state, blocking_reasons=(PlanningReasonCode.LOW_CONFIDENCE,))
            return _with_roster(build_weekly_plan(blocked, policy=config), state)
    expected = plan.expected_terminal_score
    if expected is None and comparison is not None:
        expected = comparison.selected.expected_score
    if any(
        game.sleeper_player_id in {row.player_id for row in state.observed_starters}
        and (game.sleeper_player_id, game.game_id) not in forfeitures
        for game in protected_performances(state)
    ):
        expected = None
    if PlanningReasonCode.MISSING_TIPOFF_EVIDENCE in state.warnings:
        expected = None
    result = replace(
        plan,
        observed_assignments=observed,
        moves=(*pre_moves, *plan.moves),
        fixed_slots=state.fixed_slots,
        passed_opportunities=state.passed_opportunities,
        score_actions=tuple(actions),
        pre_lock_move_count=len(pre_moves),
        recovery_obligations=tuple(
            {
                row.recovery_id: row for row in (*observed_recoveries, *plan.recovery_obligations)
            }.values()
        ),
        forfeited_opportunities=forfeitures,
        expected_terminal_score=expected,
        observed_terminal_score=comparison.baseline.expected_score
        if comparison
        else plan.observed_terminal_score,
        status=(PlanStatus.DEGRADED if state.warnings else PlanStatus.ACTION_REQUIRED)
        if pre_moves or plan.moves or actions
        else plan.status,
        confidence=PlanConfidence.MEDIUM if actions else plan.confidence,
    )
    return _with_roster(result, state)


def _with_roster(plan: WeeklyPlan, state: TeamWeekState) -> WeeklyPlan:
    """Attach every ordinary bench choice while excluding reserve-only membership."""
    starters = {row.player_id for row in plan.desired_assignments}
    if plan.status is not PlanStatus.BLOCKED and len(state.fixed_slots) == len(state.starter_slots):
        plan = replace(
            plan,
            expected_terminal_score=sum(row.accepted_fantasy_score for row in state.fixed_slots),
        )
    return replace(
        plan,
        bench_player_ids=tuple(
            sorted(set(state.roster_player_ids) - starters - set(state.reserve_player_ids))
        ),
    )
