"""Search, value, and rank concrete weekly-plan slot assignments.

The scoring coordinator supplies projection scenarios; this module applies
them to assignment candidates and resolves policy ties deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sleeper_manager.decisions._weekly_plan_inputs import opportunity_id
from sleeper_manager.decisions._weekly_plan_models import (
    PlacementEvaluation,
    WeeklyPlanOption,
)
from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    SlotAssignment,
    candidate_eligible_for_slot,
)
from sleeper_manager.decisions.simulation import (
    Scenario,
    ScenarioInput,
    rollout_scenario_assignments,
)
from sleeper_manager.domain.planning import GameOpportunity, StarterSlot, TeamWeekState

AssignmentTieKey = Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class EvaluatedAssignment:
    """Pair one current assignment with its simulated terminal value."""

    result: AssignmentResult
    expected_terminal_value: float


def enumerate_current_assignments(
    candidates: tuple[AssignmentCandidate, ...],
    open_slots: tuple[StarterSlot, ...],
) -> tuple[AssignmentResult, ...]:
    """Enumerate valid current-batch assignments, including empty slots."""

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
        """Explore every eligible placement for the slot at ``offset``."""

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


def rank_evaluations(
    evaluations: tuple[EvaluatedAssignment, ...],
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> tuple[EvaluatedAssignment, ...]:
    """Order evaluations by terminal value and deterministic policy ties."""

    remaining = list(evaluations)
    ordered: list[EvaluatedAssignment] = []
    while remaining:
        best = remaining[0]
        for candidate in remaining[1:]:
            if _better_evaluation(candidate, best, tie_key, tie_tolerance):
                best = candidate
        remaining.remove(best)
        ordered.append(best)
    return tuple(ordered)


def _better_evaluation(
    candidate: EvaluatedAssignment,
    incumbent: EvaluatedAssignment,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> bool:
    """Return whether a candidate beats the incumbent under policy rules."""

    if candidate.expected_terminal_value > incumbent.expected_terminal_value + tie_tolerance:
        return True
    if abs(candidate.expected_terminal_value - incumbent.expected_terminal_value) > tie_tolerance:
        return False
    return tie_key(candidate.result.assignments) < tie_key(incumbent.result.assignments)


def placement_evaluations(
    batch: tuple[GameOpportunity, ...],
    open_slots: tuple[StarterSlot, ...],
    candidates: tuple[AssignmentCandidate, ...],
    evaluated: tuple[EvaluatedAssignment, ...],
    baseline: float,
    *,
    tie_key: AssignmentTieKey,
    tie_tolerance: float,
) -> tuple[PlacementEvaluation, ...]:
    """Summarize each eligible player-slot placement against the baseline."""

    opportunities = {opportunity_id(opportunity): opportunity for opportunity in batch}
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
        best = rank_evaluations(
            containing,
            tie_key=tie_key,
            tie_tolerance=tie_tolerance,
        )[0]
        source_opportunity_id = candidate.candidate_id.rsplit("@slot-", 1)[0]
        opportunity = opportunities[source_opportunity_id]
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


def observed_assignment_result(
    state: TeamWeekState,
    open_slots: tuple[StarterSlot, ...],
    candidates: tuple[AssignmentCandidate, ...],
) -> AssignmentResult | None:
    """Represent observed starters that are eligible in the current batch."""

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


def assignment_terminal_value(
    assignment: AssignmentResult,
    *,
    candidates: tuple[AssignmentCandidate, ...],
    fixed_assignments: tuple[AssignmentCandidate, ...],
    future_inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
) -> float:
    """Average one current assignment's terminal score across scenarios."""

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


def option(
    assignment: AssignmentResult,
    expected_terminal_value: float,
    baseline: float,
    state: TeamWeekState,
) -> WeeklyPlanOption:
    """Build a public option summary from an evaluated assignment."""

    retained = _retained_observed_count(assignment.assignments, state)
    moves = _move_count(assignment.assignments, state)
    return WeeklyPlanOption(
        assignments=assignment.assignments,
        expected_terminal_value=round(expected_terminal_value, 6),
        marginal_value=round(expected_terminal_value - baseline, 6),
        move_count=moves,
        retained_observed_count=retained,
    )


def tie_key(state: TeamWeekState) -> AssignmentTieKey:
    """Create the stable assignment tie-breaker for a team-week state."""

    def key(assignments: tuple[SlotAssignment, ...]) -> tuple[object, ...]:
        """Prefer fewer moves, retained starters, then stable identifiers."""

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
    """Count assigned slots that differ from the observed lineup."""

    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id != observed.get(assignment.slot_index) for assignment in assignments
    )


def _retained_observed_count(
    assignments: tuple[SlotAssignment, ...],
    state: TeamWeekState,
) -> int:
    """Count assigned slots that retain their observed player."""

    observed = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    return sum(
        assignment.player_id is not None
        and assignment.player_id == observed.get(assignment.slot_index)
        for assignment in assignments
    )
