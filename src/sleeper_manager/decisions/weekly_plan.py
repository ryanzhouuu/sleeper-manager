from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    SlotAssignment,
    maximum_weight_assignment,
)
from sleeper_manager.decisions.simulation import (
    Scenario,
    ScenarioInput,
    generate_projection_scenarios,
    rollout_scenario_assignments,
    rollout_scenario_terminal_score,
    stable_scenario_seed,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    PlanningGameStatus,
    StarterSlot,
    TeamWeekState,
)


class WeeklyPlanError(ValueError):
    pass


AssignmentTieKey = Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]]


class TerminalValueApproximation(StrEnum):
    COMMON_BASELINE_MARGINAL = "common_baseline_marginal"


@dataclass(frozen=True, slots=True)
class _ContinuationAllocation:
    assignments: tuple[SlotAssignment, ...]

    def conflicting_value(self, player_id: str, slot_index: int) -> float:
        return sum(
            item.score
            for item in self.assignments
            if item.player_id == player_id or item.slot_index == slot_index
        )


@dataclass(frozen=True, slots=True)
class WeeklyPlanPolicyConfig:
    scenario_count: int = 2000
    seed: int = 0
    tie_tolerance: float = 0.01

    def __post_init__(self) -> None:
        if self.scenario_count <= 0:
            raise ValueError("Weekly plan scenario count must be positive")
        if not isfinite(self.tie_tolerance) or self.tie_tolerance < 0:
            raise ValueError("Weekly plan tie tolerance must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class PlacementEvaluation:
    candidate_id: str
    player_id: str
    game_id: str
    slot_index: int
    slot_position: str
    standalone_expected_value: float
    expected_terminal_value: float
    marginal_terminal_value: float


@dataclass(frozen=True, slots=True)
class WeeklyPlanOption:
    assignments: tuple[SlotAssignment, ...]
    expected_terminal_value: float
    marginal_value: float
    move_count: int
    retained_observed_count: int


@dataclass(frozen=True, slots=True)
class WeeklyPlanDecision:
    decision_time: datetime
    batch_start: datetime
    batch_game_ids: tuple[str, ...]
    baseline_terminal_value: float
    selected: WeeklyPlanOption
    alternative: WeeklyPlanOption | None
    scenario_count: int
    seed: int
    approximation: TerminalValueApproximation
    evaluations: tuple[PlacementEvaluation, ...]
    perfect_information_bound: float


def score_weekly_options(
    state: TeamWeekState,
    *,
    config: WeeklyPlanPolicyConfig | None = None,
) -> WeeklyPlanDecision:
    policy_config = config or WeeklyPlanPolicyConfig()
    if state.is_blocked:
        reasons = ", ".join(reason.value for reason in state.blocking_reasons)
        raise WeeklyPlanError(f"Cannot score a blocked team-week state: {reasons}")

    batch = _next_actionable_batch(state)
    batch_start = batch[0].scheduled_start
    future = _future_opportunities(state, batch_start)
    fixed_assignments = _fixed_assignments(state.fixed_slots)
    open_slots: tuple[StarterSlot, ...] = tuple(
        slot for slot in state.starter_slots if slot.index in state.open_slot_indices
    )
    scenario_inputs = tuple(_scenario_input(opportunity) for opportunity in (*batch, *future))
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
    future_inputs = tuple(_scenario_input(opportunity) for opportunity in future)
    continuation_assignments = rollout_scenario_assignments(
        fixed_assignments=fixed_assignments,
        remaining_inputs=future_inputs,
        open_slots=tuple(slot.position for slot in open_slots),
        scenarios=scenarios,
        slot_indices=tuple(slot.index for slot in open_slots),
    )
    baseline = _mean_terminal_value(
        fixed_assignments=fixed_assignments,
        continuation_assignments=continuation_assignments,
    )
    perfect_information_bound = _terminal_value(
        fixed_assignments=fixed_assignments,
        remaining_inputs=tuple(_scenario_input(opportunity) for opportunity in (*batch, *future)),
        open_slots=open_slots,
        scenarios=scenarios,
    )
    option_candidates, evaluations = _score_options(
        batch=batch,
        open_slots=open_slots,
        scenarios=scenarios,
        baseline=baseline,
        continuation_assignments=continuation_assignments,
    )
    tie_key = _tie_key(state)
    selected_assignment = _solve(
        option_candidates,
        open_slots,
        tie_key=tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    selected_value = _assignment_terminal_value(
        selected_assignment,
        candidates=option_candidates,
        fixed_assignments=fixed_assignments,
        future_inputs=future_inputs,
        open_slots=open_slots,
        scenarios=scenarios,
    )
    selected = _option(selected_assignment, selected_value, baseline, state)
    alternative_assignment = _best_alternative(
        option_candidates,
        open_slots,
        selected_assignment,
        tie_key=tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    alternative = None
    if alternative_assignment is not None:
        alternative_value = _assignment_terminal_value(
            alternative_assignment,
            candidates=option_candidates,
            fixed_assignments=fixed_assignments,
            future_inputs=future_inputs,
            open_slots=open_slots,
            scenarios=scenarios,
        )
        alternative = _option(alternative_assignment, alternative_value, baseline, state)
    return WeeklyPlanDecision(
        decision_time=state.decision_time,
        batch_start=batch_start,
        batch_game_ids=tuple(sorted({opportunity.game_id for opportunity in batch})),
        baseline_terminal_value=baseline,
        selected=selected,
        alternative=alternative,
        scenario_count=policy_config.scenario_count,
        seed=seed,
        approximation=TerminalValueApproximation.COMMON_BASELINE_MARGINAL,
        evaluations=tuple(evaluations),
        perfect_information_bound=perfect_information_bound,
    )


def _score_options(
    *,
    batch: tuple[GameOpportunity, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
    baseline: float,
    continuation_assignments: tuple[AssignmentResult, ...],
) -> tuple[tuple[AssignmentCandidate, ...], tuple[PlacementEvaluation, ...]]:
    candidates: list[AssignmentCandidate] = []
    evaluations: list[PlacementEvaluation] = []
    allocations = tuple(
        _continuation_allocation(assignment) for assignment in continuation_assignments
    )
    for opportunity in batch:
        assert opportunity.projection is not None
        for slot in open_slots:
            slot_index = slot.index
            slot_position = slot.position
            if slot_index not in opportunity.eligible_slot_indices:
                continue
            candidate_id = f"{_opportunity_id(opportunity)}@slot-{slot_index}"
            marginal = round(
                _marginal_value(
                    opportunity=opportunity,
                    slot_index=slot_index,
                    scenarios=scenarios,
                    allocations=allocations,
                ),
                6,
            )
            candidates.append(
                AssignmentCandidate(
                    candidate_id=candidate_id,
                    player_id=opportunity.sleeper_player_id,
                    score=marginal,
                    eligible_positions=opportunity.eligible_positions,
                    game_id=opportunity.game_id,
                    eligible_slot_indices=(slot_index,),
                )
            )
            evaluations.append(
                PlacementEvaluation(
                    candidate_id=candidate_id,
                    player_id=opportunity.sleeper_player_id,
                    game_id=opportunity.game_id,
                    slot_index=slot_index,
                    slot_position=slot_position,
                    standalone_expected_value=opportunity.projection.distribution.expected_value,
                    expected_terminal_value=round(baseline + marginal, 6),
                    marginal_terminal_value=marginal,
                )
            )
    return tuple(candidates), tuple(evaluations)


def _marginal_value(
    *,
    opportunity: GameOpportunity,
    scenarios: tuple[Scenario, ...],
    allocations: tuple[_ContinuationAllocation, ...],
    slot_index: int,
) -> float:
    candidate_id = _opportunity_id(opportunity)
    return sum(
        scenario.value_for(candidate_id)
        - allocation.conflicting_value(opportunity.sleeper_player_id, slot_index)
        for scenario, allocation in zip(scenarios, allocations, strict=True)
    ) / len(scenarios)


def _continuation_allocation(
    assignment: AssignmentResult,
) -> _ContinuationAllocation:
    return _ContinuationAllocation(assignment.assignments)


def _mean_terminal_value(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    continuation_assignments: tuple[AssignmentResult, ...],
) -> float:
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
    return rollout_scenario_terminal_score(
        fixed_assignments=fixed_assignments,
        remaining_inputs=remaining_inputs,
        open_slots=tuple(slot.position for slot in open_slots),
        slot_indices=tuple(slot.index for slot in open_slots),
        scenarios=scenarios,
    )


def _solve(
    candidates: tuple[AssignmentCandidate, ...],
    open_slots: tuple[StarterSlot, ...],
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
    forbidden_edges: frozenset[tuple[int, str]] = frozenset(),
    required_edges: frozenset[tuple[int, str]] = frozenset(),
) -> AssignmentResult:
    return maximum_weight_assignment(
        candidates,
        tuple(slot.position for slot in open_slots),
        slot_indices=tuple(slot.index for slot in open_slots),
        forbidden_edges=forbidden_edges,
        required_edges=required_edges,
        tie_break_key=tie_key,
        tie_tolerance=tie_tolerance,
    )


def _best_alternative(
    candidates: tuple[AssignmentCandidate, ...],
    open_slots: tuple[StarterSlot, ...],
    selected: AssignmentResult,
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> AssignmentResult | None:
    selected_ids = {
        (assignment.slot_index, assignment.candidate_id)
        for assignment in selected.assignments
        if assignment.candidate_id is not None
    }
    best: AssignmentResult | None = None
    for candidate in candidates:
        edge = next(
            (slot.index, candidate.candidate_id)
            for slot in open_slots
            if slot.index in (candidate.eligible_slot_indices or ())
        )
        if edge in selected_ids:
            continue
        result = _solve(
            candidates,
            open_slots,
            tie_key=tie_key,
            tie_tolerance=tie_tolerance,
            required_edges=frozenset({edge}),
        )
        if {
            (assignment.slot_index, assignment.candidate_id)
            for assignment in result.assignments
            if assignment.candidate_id is not None
        } != selected_ids:
            if best is None or _better_result(result, best, tie_key, tie_tolerance):
                best = result
    for slot_index, candidate_id in selected_ids:
        result = _solve(
            candidates,
            open_slots,
            tie_key=tie_key,
            tie_tolerance=tie_tolerance,
            forbidden_edges=frozenset({(slot_index, candidate_id)}),
        )
        if {
            (assignment.slot_index, assignment.candidate_id)
            for assignment in result.assignments
            if assignment.candidate_id is not None
        } != selected_ids:
            if best is None or _better_result(result, best, tie_key, tie_tolerance):
                best = result
    return best


def _better_result(
    candidate: AssignmentResult,
    incumbent: AssignmentResult,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> bool:
    if candidate.score > incumbent.score + tie_tolerance:
        return True
    if abs(candidate.score - incumbent.score) > tie_tolerance:
        return False
    return tie_key(candidate.assignments) < tie_key(incumbent.assignments)


def _assignment_terminal_value(
    assignment: AssignmentResult,
    *,
    candidates: tuple[AssignmentCandidate, ...],
    fixed_assignments: tuple[AssignmentCandidate, ...],
    future_inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
) -> float:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    fixed_score = sum(candidate.score for candidate in fixed_assignments)
    values: list[float] = []
    for scenario in scenarios:
        current_assignments: list[AssignmentCandidate] = []
        current_slots: set[int] = set()
        for item in assignment.assignments:
            if item.candidate_id is None:
                continue
            candidate = candidates_by_id[item.candidate_id]
            current_assignments.append(
                AssignmentCandidate(
                    candidate_id=candidate.candidate_id,
                    player_id=candidate.player_id,
                    score=scenario.value_for(candidate.candidate_id.rsplit("@slot-", 1)[0]),
                    eligible_positions=candidate.eligible_positions,
                    game_id=candidate.game_id,
                    eligible_slot_indices=candidate.eligible_slot_indices,
                )
            )
            current_slots.add(item.slot_index)
        remaining_slots = tuple(slot for slot in open_slots if slot.index not in current_slots)
        future_result = rollout_scenario_assignments(
            fixed_assignments=fixed_assignments + tuple(current_assignments),
            remaining_inputs=future_inputs,
            open_slots=tuple(slot.position for slot in remaining_slots),
            scenarios=(scenario,),
            slot_indices=tuple(slot.index for slot in remaining_slots),
        )[0]
        values.append(
            fixed_score
            + sum(candidate.score for candidate in current_assignments)
            + future_result.score
        )
    return round(sum(values) / len(values), 6)


def _option(
    assignment: AssignmentResult,
    expected_terminal_value: float,
    baseline: float,
    state: TeamWeekState,
) -> WeeklyPlanOption:
    retained = _retained_observed_count(assignment.assignments, state)
    moves = _move_count(assignment.assignments, state)
    return WeeklyPlanOption(
        assignments=assignment.assignments,
        expected_terminal_value=round(expected_terminal_value, 6),
        marginal_value=round(expected_terminal_value - baseline, 6),
        move_count=moves,
        retained_observed_count=retained,
    )


def _tie_key(state: TeamWeekState) -> AssignmentTieKey:

    def key(assignments: tuple[SlotAssignment, ...]) -> tuple[object, ...]:
        moves = _move_count(assignments, state)
        retained = _retained_observed_count(assignments, state)
        stable = tuple(assignment.candidate_id or "" for assignment in assignments)
        return moves, -retained, stable

    return key


def _move_count(assignments: tuple[SlotAssignment, ...], state: TeamWeekState) -> int:
    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id is not None
        and assignment.player_id != observed.get(assignment.slot_index)
        for assignment in assignments
    )


def _retained_observed_count(assignments: tuple[SlotAssignment, ...], state: TeamWeekState) -> int:
    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id is not None
        and assignment.player_id == observed.get(assignment.slot_index)
        for assignment in assignments
    )


def _next_actionable_batch(state: TeamWeekState) -> tuple[GameOpportunity, ...]:
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    candidates = tuple(
        opportunity
        for opportunity in state.opportunities
        if opportunity.status is PlanningGameStatus.SCHEDULED
        and opportunity.scheduled_start > state.decision_time
        and opportunity.rostered_at_tipoff is True
        and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
    )
    if not candidates:
        raise WeeklyPlanError("No actionable pre-tipoff opportunity remains")
    batch_start = min(opportunity.scheduled_start for opportunity in candidates)
    return tuple(
        sorted(
            (
                opportunity
                for opportunity in candidates
                if opportunity.scheduled_start == batch_start
            ),
            key=_opportunity_id,
        )
    )


def _future_opportunities(
    state: TeamWeekState, batch_start: datetime
) -> tuple[GameOpportunity, ...]:
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    return tuple(
        sorted(
            (
                opportunity
                for opportunity in state.opportunities
                if opportunity.scheduled_start > batch_start
                and opportunity.status in (PlanningGameStatus.SCHEDULED, PlanningGameStatus.ACTIVE)
                and opportunity.rostered_at_tipoff is True
                and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
            ),
            key=_opportunity_id,
        )
    )


def _scenario_input(opportunity: GameOpportunity) -> ScenarioInput:
    if opportunity.projection is None:
        raise WeeklyPlanError(
            f"Missing projection for {opportunity.sleeper_player_id}:{opportunity.game_id}"
        )
    return ScenarioInput(
        candidate_id=_opportunity_id(opportunity),
        player_id=opportunity.sleeper_player_id,
        game_id=opportunity.game_id,
        eligible_positions=opportunity.eligible_positions,
        projection=opportunity.projection,
        eligible_slot_indices=opportunity.eligible_slot_indices,
    )


def _fixed_assignments(fixed_slots: Iterable[FixedSlot]) -> tuple[AssignmentCandidate, ...]:
    return tuple(
        AssignmentCandidate(
            candidate_id=f"fixed:{fixed.slot_index}:{fixed.player_id}:{fixed.game_id}",
            player_id=fixed.player_id,
            score=fixed.accepted_fantasy_score,
            eligible_positions=(fixed.slot_position,),
            game_id=fixed.game_id,
            eligible_slot_indices=(fixed.slot_index,),
        )
        for fixed in fixed_slots
    )


def _opportunity_id(opportunity: GameOpportunity) -> str:
    return (
        f"{opportunity.sleeper_player_id}:{opportunity.game_id}:"
        f"{opportunity.membership_segment or opportunity.roster_id}"
    )


__all__ = (
    "PlacementEvaluation",
    "TerminalValueApproximation",
    "WeeklyPlanDecision",
    "WeeklyPlanError",
    "WeeklyPlanOption",
    "WeeklyPlanPolicyConfig",
    "score_weekly_options",
)
