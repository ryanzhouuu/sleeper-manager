"""Public weekly-plan orchestration and compatibility exports.

Callers use this module for planner configuration, option scoring, and final
lineup plans while private sibling modules own the extracted implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from sleeper_manager.decisions._weekly_plan_inputs import (
    future_opportunities,
    next_actionable_batch,
    opportunity_id,
)
from sleeper_manager.decisions._weekly_plan_models import (
    DEFAULT_MOVE_LEAD_TIME,
    WEEKLY_PLANNER_VERSION,
    PlacementEvaluation,
    TerminalValueApproximation,
    WeeklyPlanDecision,
    WeeklyPlanError,
    WeeklyPlanOption,
    WeeklyPlanPolicyConfig,
)
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions._weekly_plan_scoring import score_weekly_options
from sleeper_manager.domain.planning import (
    GameOpportunity,
    PassedOpportunity,
    PlanConfidence,
    PlanDistributionSummary,
    PlannedAssignment,
    PlanningQuality,
    PlanningReasonCode,
    PlanStatus,
    TeamWeekState,
    WeeklyPlan,
)


def build_weekly_plan(
    state: TeamWeekState,
    *,
    lead_time: timedelta = DEFAULT_MOVE_LEAD_TIME,
    policy: WeeklyPlanPolicyConfig | None = None,
    planner_version: str = WEEKLY_PLANNER_VERSION,
) -> WeeklyPlan:
    policy_config = policy or WeeklyPlanPolicyConfig()
    observed_view, slot_positions = _observed_assignment_view(state)
    if state.is_blocked:
        return _static_plan(
            state,
            observed_view,
            slot_positions,
            PlanStatus.BLOCKED,
            (),
            planner_version,
        )

    batch = next_actionable_batch(state)
    if batch is None:
        return _static_plan(
            state,
            observed_view,
            slot_positions,
            PlanStatus.NO_ACTION,
            (),
            planner_version,
        )
    future = future_opportunities(state, batch[0].scheduled_start)
    if any(opportunity.projection is None for opportunity in (*batch, *future)):
        return _static_plan(
            state,
            observed_view,
            slot_positions,
            PlanStatus.BLOCKED,
            (PlanningReasonCode.MISSING_PROJECTION,),
            planner_version,
        )

    decision = score_weekly_options(state, config=policy_config)
    desired_players = {
        assignment.slot_index: assignment.player_id for assignment in decision.selected.assignments
    }
    desired_players.update({fixed.slot_index: fixed.player_id for fixed in state.fixed_slots})
    desired_view = _ordered_view(slot_positions, desired_players)
    moves = plan_moves(state, desired_view, batch[0].scheduled_start, lead_time)

    blocking_reasons: tuple[PlanningReasonCode, ...] = ()
    status = PlanStatus.ACTION_REQUIRED if moves else PlanStatus.NO_ACTION
    if state.warnings:
        status = PlanStatus.DEGRADED
    if any(move.deadline <= state.decision_time for move in moves):
        status = PlanStatus.BLOCKED
        blocking_reasons = (PlanningReasonCode.DEADLINE_ELAPSED,)

    alternative_score = (
        decision.alternative.expected_terminal_value
        if decision.alternative is not None
        else decision.observed_terminal_value
    )
    margin = round(decision.selected.expected_terminal_value - alternative_score, 6)
    return WeeklyPlan(
        league_id=state.league_id,
        season=state.season,
        week=state.week,
        roster_id=state.roster_id,
        decision_time=state.decision_time,
        status=status,
        observed_assignments=observed_view,
        desired_assignments=desired_view,
        moves=moves,
        confidence=_plan_confidence(
            state.eligibility_quality,
            margin,
            policy_config.tie_tolerance,
        ),
        planner_version=planner_version,
        manager_policy_version=state.manager_policy_version,
        scoring_policy_version=state.scoring_policy_version,
        league_configuration_version=state.league_configuration_version,
        projection_model_version=state.projection_model_version,
        input_version=state.input_version,
        expected_terminal_score=decision.selected.expected_terminal_value,
        best_alternative_score=alternative_score,
        observed_terminal_score=decision.observed_terminal_value,
        decision_margin=margin,
        distribution_summary=PlanDistributionSummary(
            scenario_count=decision.scenario_count,
            seed=decision.seed,
            approximation=decision.approximation.value,
            perfect_information_bound=decision.perfect_information_bound,
        ),
        fixed_slots=tuple(sorted(state.fixed_slots, key=lambda fixed: fixed.slot_index)),
        passed_opportunities=tuple(sorted(state.passed_opportunities, key=_passed_sort_key)),
        schedule_assumptions=_schedule_assumptions(batch, future),
        freshness=state.freshness,
        warnings=state.warnings,
        blocking_reasons=blocking_reasons,
    )


def _static_plan(
    state: TeamWeekState,
    observed_view: tuple[PlannedAssignment, ...],
    slot_positions: dict[int, str],
    status: PlanStatus,
    extra_blocking: tuple[PlanningReasonCode, ...],
    planner_version: str,
) -> WeeklyPlan:
    observed_players = {assignment.slot_index: assignment.player_id for assignment in observed_view}
    for fixed in state.fixed_slots:
        observed_players[fixed.slot_index] = fixed.player_id
    return WeeklyPlan(
        league_id=state.league_id,
        season=state.season,
        week=state.week,
        roster_id=state.roster_id,
        decision_time=state.decision_time,
        status=status,
        observed_assignments=observed_view,
        desired_assignments=_ordered_view(slot_positions, observed_players),
        moves=(),
        confidence=PlanConfidence.LOW,
        planner_version=planner_version,
        manager_policy_version=state.manager_policy_version,
        scoring_policy_version=state.scoring_policy_version,
        league_configuration_version=state.league_configuration_version,
        projection_model_version=state.projection_model_version,
        input_version=state.input_version,
        fixed_slots=tuple(sorted(state.fixed_slots, key=lambda fixed: fixed.slot_index)),
        passed_opportunities=tuple(sorted(state.passed_opportunities, key=_passed_sort_key)),
        freshness=state.freshness,
        warnings=state.warnings,
        blocking_reasons=state.blocking_reasons + extra_blocking,
    )


def _observed_assignment_view(
    state: TeamWeekState,
) -> tuple[tuple[PlannedAssignment, ...], dict[int, str]]:
    slot_positions = {slot.index: slot.position for slot in state.starter_slots}
    observed_players = {
        starter.slot_index: starter.player_id for starter in state.observed_starters
    }
    return _ordered_view(slot_positions, observed_players), slot_positions


def _ordered_view(
    slot_positions: dict[int, str],
    players_by_slot: Mapping[int, str | None],
) -> tuple[PlannedAssignment, ...]:
    return tuple(
        PlannedAssignment(index, slot_positions[index], players_by_slot.get(index))
        for index in sorted(slot_positions)
    )


def _plan_confidence(
    quality: PlanningQuality,
    margin: float,
    tie_tolerance: float,
) -> PlanConfidence:
    if quality in (PlanningQuality.PARTIAL, PlanningQuality.UNKNOWN):
        return PlanConfidence.LOW
    if quality is PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE:
        return PlanConfidence.MEDIUM
    return PlanConfidence.HIGH if abs(margin) > tie_tolerance else PlanConfidence.MEDIUM


def _schedule_assumptions(
    batch: tuple[GameOpportunity, ...],
    future: tuple[GameOpportunity, ...],
) -> tuple[str, ...]:
    assumptions = [
        f"game {opportunity.game_id} assumed to start {opportunity.scheduled_start.isoformat()}"
        for opportunity in sorted(batch, key=opportunity_id)
    ]
    assumptions.append(f"{len(future)} later opportunities remain replannable")
    return tuple(assumptions)


def _passed_sort_key(passed: PassedOpportunity) -> tuple[str, str]:
    return passed.player_id, passed.game_id


__all__ = (
    "DEFAULT_MOVE_LEAD_TIME",
    "PlacementEvaluation",
    "TerminalValueApproximation",
    "WEEKLY_PLANNER_VERSION",
    "WeeklyPlanDecision",
    "WeeklyPlanError",
    "WeeklyPlanOption",
    "WeeklyPlanPolicyConfig",
    "build_weekly_plan",
    "score_weekly_options",
)

for _public_type in (
    PlacementEvaluation,
    TerminalValueApproximation,
    WeeklyPlanDecision,
    WeeklyPlanError,
    WeeklyPlanOption,
    WeeklyPlanPolicyConfig,
):
    _public_type.__module__ = __name__
del _public_type
