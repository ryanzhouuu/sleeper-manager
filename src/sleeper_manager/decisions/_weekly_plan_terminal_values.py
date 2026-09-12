"""Map concrete weekly choices onto exact shared continuation values.

Concrete assignments remain the planner's policy and move-generation surface;
this module groups only states with identical terminal scoring semantics.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sleeper_manager.decisions._weekly_plan_continuations import (
    PreparedContinuationTopology,
)
from sleeper_manager.decisions.lineup import AssignmentCandidate, AssignmentResult
from sleeper_manager.decisions.simulation import Scenario, ScenarioInput, SimulationError
from sleeper_manager.domain.planning import StarterSlot


@dataclass(frozen=True, slots=True)
class _SemanticState:
    """Identify current choices that have the same terminal continuation value."""

    selected_candidate_ids: tuple[str, ...]
    occupied_slot_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _StateEvaluation:
    """Carry representative summation order and lookup data for one semantic state."""

    key: _SemanticState
    sampled_candidate_ids: tuple[str, ...]
    selected_player_ids: frozenset[str]
    remaining_slot_mask: int


def assignment_terminal_values(
    assignments: Sequence[AssignmentResult],
    *,
    candidates: tuple[AssignmentCandidate, ...],
    fixed_assignments: tuple[AssignmentCandidate, ...],
    future_inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
    ignored_candidate_ids: frozenset[str] = frozenset(),
) -> dict[AssignmentResult, float]:
    """Evaluate concrete assignments through exact shared continuation tables."""

    if not scenarios:
        raise SimulationError("Scenario assignment rollout requires scenarios")
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    slot_bits = {slot.index: 1 << position for position, slot in enumerate(open_slots)}
    topology = PreparedContinuationTopology(future_inputs, open_slots)
    states: dict[_SemanticState, _StateEvaluation] = {}
    assignment_keys: dict[AssignmentResult, _SemanticState] = {}
    for assignment in assignments:
        selected = tuple(
            candidates_by_id[item.candidate_id]
            for item in assignment.assignments
            if item.candidate_id is not None and item.candidate_id not in ignored_candidate_ids
        )
        sampled_candidate_ids = tuple(
            candidate.candidate_id.rsplit("@slot-", 1)[0] for candidate in selected
        )
        occupied_slot_indices = tuple(
            sorted(
                item.slot_index
                for item in assignment.assignments
                if item.candidate_id is not None and item.candidate_id not in ignored_candidate_ids
            )
        )
        key = _SemanticState(tuple(sorted(sampled_candidate_ids)), occupied_slot_indices)
        assignment_keys[assignment] = key
        if key in states:
            continue
        occupied_mask = sum(slot_bits[index] for index in occupied_slot_indices)
        states[key] = _StateEvaluation(
            key=key,
            sampled_candidate_ids=sampled_candidate_ids,
            selected_player_ids=frozenset(candidate.player_id for candidate in selected),
            remaining_slot_mask=topology.full_slot_mask & ~occupied_mask,
        )

    states_by_players: dict[frozenset[str], list[_StateEvaluation]] = {}
    for state in states.values():
        relevant_players = state.selected_player_ids & topology.player_ids
        states_by_players.setdefault(relevant_players, []).append(state)
    fixed_score = sum(candidate.score for candidate in fixed_assignments)
    fixed_players = (
        frozenset(candidate.player_id for candidate in fixed_assignments) & topology.player_ids
    )
    totals = {key: 0.0 for key in states}
    for scenario in scenarios:
        scenario_values = dict(scenario.values)
        requested_masks_by_exclusion = {
            fixed_players | selected_players: frozenset(
                state.remaining_slot_mask for state in grouped_states
            )
            for selected_players, grouped_states in states_by_players.items()
        }
        continuation_tables = topology.best_scores_by_excluded_players(
            scenario_values,
            requested_masks_by_exclusion,
        )
        for selected_players, grouped_states in states_by_players.items():
            continuation_scores = continuation_tables[fixed_players | selected_players]
            for state in grouped_states:
                current_score = sum(
                    scenario_values.get(candidate_id, 0.0)
                    for candidate_id in state.sampled_candidate_ids
                )
                totals[state.key] += (
                    fixed_score + current_score + continuation_scores[state.remaining_slot_mask]
                )

    values_by_key = {key: round(total / len(scenarios), 6) for key, total in totals.items()}
    return {assignment: values_by_key[key] for assignment, key in assignment_keys.items()}
