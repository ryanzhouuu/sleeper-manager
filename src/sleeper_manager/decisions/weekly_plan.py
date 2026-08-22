from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite

from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    SlotAssignment,
    candidate_eligible_for_slot,
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
    LineupMove,
    PassedOpportunity,
    PlanConfidence,
    PlanDistributionSummary,
    PlannedAssignment,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
    PlanStatus,
    StarterSlot,
    TeamWeekState,
    WeeklyPlan,
)

WEEKLY_PLANNER_VERSION = "weekly-planner-v1"
DEFAULT_MOVE_LEAD_TIME = timedelta(minutes=10)


class WeeklyPlanError(ValueError):
    pass


AssignmentTieKey = Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]]


class TerminalValueApproximation(StrEnum):
    COMMON_BASELINE_MARGINAL = "common_baseline_marginal"
    COMPLETE_ASSIGNMENT_ROLLOUT = "complete_assignment_rollout"


@dataclass(frozen=True, slots=True)
class _EvaluatedAssignment:
    result: AssignmentResult
    expected_terminal_value: float


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
    observed_terminal_value: float
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
    if batch is None:
        raise WeeklyPlanError("No actionable pre-tipoff opportunity remains")
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
    option_candidates = _option_candidates(batch, open_slots)
    assignments = _enumerate_current_assignments(option_candidates, open_slots)
    tie_key = _tie_key(state)
    evaluated = tuple(
        _EvaluatedAssignment(
            assignment,
            _assignment_terminal_value(
                assignment,
                candidates=option_candidates,
                fixed_assignments=fixed_assignments,
                future_inputs=future_inputs,
                open_slots=open_slots,
                scenarios=scenarios,
            ),
        )
        for assignment in assignments
    )
    ordered = _rank_evaluations(
        evaluated,
        tie_key=tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    selected_evaluation = ordered[0]
    selected = _option(
        selected_evaluation.result,
        selected_evaluation.expected_terminal_value,
        baseline,
        state,
    )
    alternative = None
    if len(ordered) > 1:
        alternative_evaluation = ordered[1]
        alternative = _option(
            alternative_evaluation.result,
            alternative_evaluation.expected_terminal_value,
            baseline,
            state,
        )
    evaluations = _placement_evaluations(
        batch,
        open_slots,
        option_candidates,
        evaluated,
        baseline,
        tie_key=tie_key,
        tie_tolerance=policy_config.tie_tolerance,
    )
    observed_result = _observed_assignment_result(state, open_slots, option_candidates)
    observed_value = (
        _assignment_terminal_value(
            observed_result,
            candidates=option_candidates,
            fixed_assignments=fixed_assignments,
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

    batch = _next_actionable_batch(state)
    if batch is None:
        return _static_plan(
            state,
            observed_view,
            slot_positions,
            PlanStatus.NO_ACTION,
            (),
            planner_version,
        )
    future = _future_opportunities(state, batch[0].scheduled_start)
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
    moves = _plan_moves(state, desired_view, batch[0].scheduled_start, lead_time)

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


def _plan_moves(
    state: TeamWeekState,
    desired_view: tuple[PlannedAssignment, ...],
    batch_start: datetime,
    lead_time: timedelta,
) -> tuple[LineupMove, ...]:
    fixed_indices = {fixed.slot_index for fixed in state.fixed_slots}
    open_indices = [slot.index for slot in state.starter_slots if slot.index not in fixed_indices]
    observed_players = {
        starter.slot_index: starter.player_id for starter in state.observed_starters
    }
    desired_players = {assignment.slot_index: assignment.player_id for assignment in desired_view}

    pending: dict[str, list[int | None]] = {}

    def pending_move(player_id: str) -> list[int | None]:
        return pending.setdefault(player_id, [None, None])

    for index in open_indices:
        current = observed_players.get(index)
        wanted = desired_players.get(index)
        if current == wanted:
            continue
        if current is not None:
            pending_move(current)[0] = index
        if wanted is not None:
            pending_move(wanted)[1] = index

    occupied = {
        index: player for index, player in observed_players.items() if index in open_indices
    }
    # Every step must land before the earliest tipoff the sequence depends on,
    # so a swap's bench step cannot carry a later deadline than its dependent fill.
    sequence_deadline = min(
        (_move_deadline(state, player_id, batch_start, lead_time) for player_id in pending),
        default=batch_start,
    )
    emitted: list[LineupMove] = []
    while pending:
        progressed = False
        for player_id in sorted(pending):
            source, target = pending[player_id]
            if target is not None and target in occupied:
                continue
            emitted.append(
                LineupMove(
                    player_id=player_id,
                    source_slot_index=source,
                    target_slot_index=target,
                    deadline=sequence_deadline,
                )
            )
            if source is not None:
                occupied.pop(source)
            if target is not None:
                occupied[target] = player_id
            del pending[player_id]
            progressed = True
        if pending and not progressed:
            player_id = min(pending)
            source, target = pending.pop(player_id)
            emitted.append(
                LineupMove(
                    player_id=player_id,
                    source_slot_index=source,
                    target_slot_index=None,
                    deadline=sequence_deadline,
                )
            )
            if source is not None:
                occupied.pop(source)
            pending[player_id] = [None, target]
    return tuple(emitted)


def _move_deadline(
    state: TeamWeekState,
    player_id: str,
    batch_start: datetime,
    lead_time: timedelta,
) -> datetime:
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    starts = sorted(
        opportunity.scheduled_start
        for opportunity in state.opportunities
        if opportunity.sleeper_player_id == player_id
        and opportunity.status is PlanningGameStatus.SCHEDULED
        and opportunity.scheduled_start > state.decision_time
        and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
    )
    earliest = starts[0] if starts else batch_start
    return earliest - lead_time


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
        for opportunity in sorted(batch, key=_opportunity_id)
    ]
    assumptions.append(f"{len(future)} later opportunities remain replannable")
    return tuple(assumptions)


def _passed_sort_key(passed: PassedOpportunity) -> tuple[str, str]:
    return passed.player_id, passed.game_id


def _option_candidates(
    batch: tuple[GameOpportunity, ...],
    open_slots: tuple[StarterSlot, ...],
) -> tuple[AssignmentCandidate, ...]:
    candidates: list[AssignmentCandidate] = []
    for opportunity in batch:
        assert opportunity.projection is not None
        for slot in open_slots:
            slot_index = slot.index
            if slot_index not in opportunity.eligible_slot_indices:
                continue
            candidate_id = f"{_opportunity_id(opportunity)}@slot-{slot_index}"
            candidates.append(
                AssignmentCandidate(
                    candidate_id=candidate_id,
                    player_id=opportunity.sleeper_player_id,
                    score=opportunity.projection.distribution.expected_value,
                    eligible_positions=opportunity.eligible_positions,
                    game_id=opportunity.game_id,
                    eligible_slot_indices=(slot_index,),
                )
            )
    return tuple(candidates)


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


def _enumerate_current_assignments(
    candidates: tuple[AssignmentCandidate, ...],
    open_slots: tuple[StarterSlot, ...],
) -> tuple[AssignmentResult, ...]:
    candidates_by_slot = tuple(
        tuple(
            candidate
            for candidate in sorted(candidates, key=lambda item: item.candidate_id)
            if candidate_eligible_for_slot(
                candidate,
                slot_index=slot.index,
                slot_position=slot.position,
            )
        )
        for slot in open_slots
    )
    results: list[AssignmentResult] = []

    def visit(
        offset: int,
        used_players: frozenset[str],
        assignments: tuple[SlotAssignment, ...],
        score: float,
    ) -> None:
        if offset == len(open_slots):
            results.append(AssignmentResult(round(score, 6), assignments))
            return
        slot = open_slots[offset]
        visit(
            offset + 1,
            used_players,
            assignments + (SlotAssignment(slot.index, slot.position, None, None, None, 0.0),),
            score,
        )
        for candidate in candidates_by_slot[offset]:
            if candidate.player_id in used_players:
                continue
            visit(
                offset + 1,
                used_players | {candidate.player_id},
                assignments
                + (
                    SlotAssignment(
                        slot.index,
                        slot.position,
                        candidate.candidate_id,
                        candidate.player_id,
                        candidate.game_id,
                        candidate.score,
                    ),
                ),
                score + candidate.score,
            )

    visit(0, frozenset(), (), 0.0)
    return tuple(results)


def _rank_evaluations(
    evaluations: tuple[_EvaluatedAssignment, ...],
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> tuple[_EvaluatedAssignment, ...]:
    remaining = list(evaluations)
    ordered: list[_EvaluatedAssignment] = []
    while remaining:
        best = remaining[0]
        for candidate in remaining[1:]:
            if _better_evaluation(candidate, best, tie_key, tie_tolerance):
                best = candidate
        remaining.remove(best)
        ordered.append(best)
    return tuple(ordered)


def _better_evaluation(
    candidate: _EvaluatedAssignment,
    incumbent: _EvaluatedAssignment,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> bool:
    if candidate.expected_terminal_value > incumbent.expected_terminal_value + tie_tolerance:
        return True
    if abs(candidate.expected_terminal_value - incumbent.expected_terminal_value) > tie_tolerance:
        return False
    return tie_key(candidate.result.assignments) < tie_key(incumbent.result.assignments)


def _placement_evaluations(
    batch: tuple[GameOpportunity, ...],
    open_slots: tuple[StarterSlot, ...],
    candidates: tuple[AssignmentCandidate, ...],
    evaluated: tuple[_EvaluatedAssignment, ...],
    baseline: float,
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> tuple[PlacementEvaluation, ...]:
    opportunities = {_opportunity_id(opportunity): opportunity for opportunity in batch}
    slots = {slot.index: slot for slot in open_slots}
    results: list[PlacementEvaluation] = []
    for candidate in candidates:
        slot_index = (candidate.eligible_slot_indices or ())[0]
        containing = tuple(
            evaluation
            for evaluation in evaluated
            if any(
                assignment.candidate_id == candidate.candidate_id
                for assignment in evaluation.result.assignments
            )
        )
        best = _rank_evaluations(
            containing,
            tie_key=tie_key,
            tie_tolerance=tie_tolerance,
        )[0]
        opportunity_id = candidate.candidate_id.rsplit("@slot-", 1)[0]
        opportunity = opportunities[opportunity_id]
        expected_terminal = best.expected_terminal_value
        results.append(
            PlacementEvaluation(
                candidate_id=candidate.candidate_id,
                player_id=candidate.player_id,
                game_id=candidate.game_id or opportunity.game_id,
                slot_index=slot_index,
                slot_position=slots[slot_index].position,
                standalone_expected_value=opportunity.projection.distribution.expected_value
                if opportunity.projection is not None
                else 0.0,
                expected_terminal_value=expected_terminal,
                marginal_terminal_value=round(expected_terminal - baseline, 6),
            )
        )
    return tuple(results)


def _observed_assignment_result(
    state: TeamWeekState,
    open_slots: tuple[StarterSlot, ...],
    candidates: tuple[AssignmentCandidate, ...],
) -> AssignmentResult | None:
    observed_by_slot = {
        starter.slot_index: starter.player_id for starter in state.observed_starters
    }
    candidate_by_player_slot = {
        (candidate.player_id, candidate.eligible_slot_indices[0]): candidate
        for candidate in candidates
        if candidate.eligible_slot_indices
    }
    assignments: list[SlotAssignment] = []
    for slot in open_slots:
        player_id = observed_by_slot.get(slot.index)
        if player_id is None:
            continue
        candidate = candidate_by_player_slot.get((player_id, slot.index))
        if candidate is None:
            continue
        assignments.append(
            SlotAssignment(
                slot_index=slot.index,
                slot_position=slot.position,
                candidate_id=candidate.candidate_id,
                player_id=candidate.player_id,
                game_id=candidate.game_id,
                score=candidate.score,
            )
        )
    if not assignments:
        return None
    return AssignmentResult(0.0, tuple(assignments))


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
        stable = tuple(
            sorted(
                (
                    assignment.player_id or "",
                    assignment.slot_index,
                    assignment.candidate_id or "",
                )
                for assignment in assignments
                if assignment.player_id is not None
            )
        )
        return moves, -retained, stable

    return key


def _move_count(assignments: tuple[SlotAssignment, ...], state: TeamWeekState) -> int:
    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id != observed.get(assignment.slot_index) for assignment in assignments
    )


def _retained_observed_count(assignments: tuple[SlotAssignment, ...], state: TeamWeekState) -> int:
    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id is not None
        and assignment.player_id == observed.get(assignment.slot_index)
        for assignment in assignments
    )


def _next_actionable_batch(state: TeamWeekState) -> tuple[GameOpportunity, ...] | None:
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
        return None
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
