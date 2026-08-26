"""Coordinate weekly-plan scoring across projection scenarios.

This module prepares scenarios and delegates concrete assignment evaluation to
the assignment-search helpers before returning the public decision model.
"""

from __future__ import annotations

from sleeper_manager.decisions._weekly_plan_evaluations import (
    EvaluatedAssignment,
    assignment_terminal_value,
    enumerate_current_assignments,
    observed_assignment_result,
    option,
    placement_evaluations,
    rank_evaluations,
    tie_key,
)
from sleeper_manager.decisions._weekly_plan_inputs import (
    fixed_assignments,
    future_opportunities,
    next_actionable_batch,
    option_candidates,
    scenario_input,
)
from sleeper_manager.decisions._weekly_plan_models import (
    TerminalValueApproximation,
    WeeklyPlanDecision,
    WeeklyPlanError,
    WeeklyPlanPolicyConfig,
)
from sleeper_manager.decisions.lineup import AssignmentCandidate, AssignmentResult
from sleeper_manager.decisions.simulation import (
    Scenario,
    ScenarioInput,
    generate_projection_scenarios,
    rollout_scenario_assignments,
    rollout_scenario_terminal_score,
    stable_scenario_seed,
)
from sleeper_manager.domain.planning import StarterSlot, TeamWeekState


def score_weekly_options(
    state: TeamWeekState,
    *,
    config: WeeklyPlanPolicyConfig | None = None,
) -> WeeklyPlanDecision:
    """Rank current-batch assignments against simulated continuation value.

    Raises:
        WeeklyPlanError: If the state is blocked or has no actionable opportunity.
    """

    policy_config = config or WeeklyPlanPolicyConfig()
    if state.is_blocked:
        reasons = ", ".join(reason.value for reason in state.blocking_reasons)
        raise WeeklyPlanError(f"Cannot score a blocked team-week state: {reasons}")

    batch = next_actionable_batch(state)
    if batch is None:
        raise WeeklyPlanError("No actionable pre-tipoff opportunity remains")
    batch_start = batch[0].scheduled_start
    future = future_opportunities(state, batch_start)
    fixed_assignment_candidates = fixed_assignments(state.fixed_slots)
    open_slots: tuple[StarterSlot, ...] = tuple(
        slot for slot in state.starter_slots if slot.index in state.open_slot_indices
    )
    scenario_inputs = tuple(scenario_input(opportunity) for opportunity in (*batch, *future))
    seed = stable_scenario_seed(
        policy_config.seed,
        league_id=state.league_id,
        week=state.week,
        roster_id=state.roster_id,
        decision_time=state.decision_time,
    )
    scenarios = generate_projection_scenarios(
        scenario_inputs,
        decision_time=state.decision_time,
        count=policy_config.scenario_count,
        seed=seed,
    )
    future_inputs = tuple(scenario_input(opportunity) for opportunity in future)
    continuation_assignments = rollout_scenario_assignments(
        fixed_assignments=fixed_assignment_candidates,
        remaining_inputs=future_inputs,
        open_slots=tuple(slot.position for slot in open_slots),
        scenarios=scenarios,
        slot_indices=tuple(slot.index for slot in open_slots),
    )
    baseline = _mean_terminal_value(
        fixed_assignments=fixed_assignment_candidates,
        continuation_assignments=continuation_assignments,
    )
    perfect_information_bound = _terminal_value(
        fixed_assignments=fixed_assignment_candidates,
        remaining_inputs=tuple(scenario_input(opportunity) for opportunity in (*batch, *future)),
        open_slots=open_slots,
        scenarios=scenarios,
    )
    candidates = option_candidates(batch, open_slots)
    assignments = enumerate_current_assignments(candidates, open_slots)
    assignment_tie_key = tie_key(state)
    evaluated = tuple(
        EvaluatedAssignment(
            assignment,
            assignment_terminal_value(
                assignment,
                candidates=candidates,
                fixed_assignments=fixed_assignment_candidates,
                future_inputs=future_inputs,
                open_slots=open_slots,
                scenarios=scenarios,
            ),
        )
        for assignment in assignments
    )
    ordered = rank_evaluations(
        evaluated,
        tie_key=assignment_tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    selected_evaluation = ordered[0]
    selected = option(
        selected_evaluation.result,
        selected_evaluation.expected_terminal_value,
        baseline,
        state,
    )
    alternative = None
    if len(ordered) > 1:
        alternative_evaluation = ordered[1]
        alternative = option(
            alternative_evaluation.result,
            alternative_evaluation.expected_terminal_value,
            baseline,
            state,
        )
    evaluations = placement_evaluations(
        batch,
        open_slots,
        candidates,
        evaluated,
        baseline,
        tie_key=assignment_tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    observed_result = observed_assignment_result(state, open_slots, candidates)
    observed_value = (
        assignment_terminal_value(
            observed_result,
            candidates=candidates,
            fixed_assignments=fixed_assignment_candidates,
            future_inputs=future_inputs,
            open_slots=open_slots,
            scenarios=scenarios,
        )
        if observed_result is not None
        else baseline
    )
    return WeeklyPlanDecision(
        decision_time=state.decision_time,
        batch_start=batch_start,
        batch_game_ids=tuple(sorted({opportunity.game_id for opportunity in batch})),
        baseline_terminal_value=baseline,
        observed_terminal_value=observed_value,
        selected=selected,
        alternative=alternative,
        scenario_count=policy_config.scenario_count,
        seed=seed,
        approximation=TerminalValueApproximation.COMPLETE_ASSIGNMENT_ROLLOUT,
        evaluations=evaluations,
        perfect_information_bound=perfect_information_bound,
    )


def _mean_terminal_value(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    continuation_assignments: tuple[AssignmentResult, ...],
) -> float:
    """Average continuation scores and include already fixed starter value."""

    fixed_score = sum(candidate.score for candidate in fixed_assignments)
    return round(
        fixed_score
        + sum(assignment.score for assignment in continuation_assignments)
        / len(continuation_assignments),
        6,
    )


def _terminal_value(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    remaining_inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
) -> float:
    """Compute the perfect-information terminal-score bound for open slots."""

    return rollout_scenario_terminal_score(
        fixed_assignments=fixed_assignments,
        remaining_inputs=remaining_inputs,
        open_slots=tuple(slot.position for slot in open_slots),
        slot_indices=tuple(slot.index for slot in open_slots),
        scenarios=scenarios,
    )
