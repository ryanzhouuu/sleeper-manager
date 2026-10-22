"""Value a bounded observe/Lock/Pass stage before the future assignment approximation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil

from sleeper_manager.decisions._lock_flexibility import future_player_sets
from sleeper_manager.decisions._weekly_plan_continuations import PreparedContinuationTopology
from sleeper_manager.decisions._weekly_plan_lock_targets import LockChoices
from sleeper_manager.decisions.lineup import AssignmentCandidate, AssignmentResult
from sleeper_manager.decisions.simulation import Scenario, ScenarioInput, SimulationError
from sleeper_manager.domain.planning import StarterSlot


@dataclass(frozen=True, slots=True)
class _Continuation:
    """Identify future capacity after retaining a subset of observed scores."""

    excluded: frozenset[str]
    slot_mask: int


@dataclass(frozen=True, slots=True)
class _Placement:
    """Keep the current lock slot separate from the shared sampled game identity."""

    sample_id: str
    player_id: str
    slot_bit: int


@dataclass(frozen=True, slots=True)
class _Choice:
    """Describe one accepted subset and its independently estimated lock thresholds."""

    sample_ids: tuple[str, ...]
    continuation: _Continuation
    expected_future: float
    thresholds: tuple[tuple[str, float], ...]
    flexibility: frozenset[int] = frozenset()


def recourse_terminal_values(
    assignments: Sequence[AssignmentResult],
    *,
    candidates: tuple[AssignmentCandidate, ...],
    fixed_assignments: tuple[AssignmentCandidate, ...],
    future_inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    scenarios: tuple[Scenario, ...],
    postgame_scenarios: tuple[Scenario, ...],
    minimum_confidence: float,
    tie_tolerance: float,
    ignored_candidate_ids: frozenset[str] = frozenset(),
    placement_options: Mapping[AssignmentResult, LockChoices] | None = None,
    score_samples: dict[AssignmentResult, tuple[float, ...]] | None = None,
) -> dict[AssignmentResult, float]:
    """Choose joint batch locks using inner draws, then score them on outer draws.

    Supplied placements include executable pre-lock realignment. The stage resolves the batch
    together and leaves later opportunities to the existing hindsight assignment
    approximation; it does not simulate later reforecasting or repeated Waits.
    """
    if not scenarios or not postgame_scenarios:
        raise SimulationError("Postgame recourse rollout requires scenarios")
    topology = PreparedContinuationTopology(future_inputs, open_slots)
    slot_bits = {slot.index: 1 << i for i, slot in enumerate(open_slots)}
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    assignment_keys = {
        assignment: tuple(
            sorted(
                _placements(assignment, by_id, slot_bits, ignored_candidate_ids),
                key=lambda item: (item.player_id, item.sample_id, item.slot_bit),
            )
        )
        for assignment in assignments
    }
    fixed_players = frozenset(item.player_id for item in fixed_assignments)
    subsets = {
        key: tuple(
            tuple(item for i, item in enumerate(key) if mask & (1 << i))
            for mask in range(1 << len(key))
        )
        for key in set(assignment_keys.values())
    }
    if placement_options:
        for assignment, options in placement_options.items():
            key = assignment_keys[assignment]
            subsets[key] = tuple(
                tuple(
                    _Placement(sample, player, slot_bits[index]) for sample, player, index in choice
                )
                for choice in options
            )
    continuation_subsets = {subset for options in subsets.values() for subset in options}
    for subset in tuple(continuation_subsets):
        continuation_subsets.update(
            tuple(item for index, item in enumerate(subset) if mask & (1 << index))
            for mask in range(1 << len(subset))
        )
    continuations = {
        subset: _Continuation(
            (fixed_players | frozenset(item.player_id for item in subset)) & topology.player_ids,
            topology.full_slot_mask & ~sum(item.slot_bit for item in subset),
        )
        for subset in continuation_subsets
    }
    requested: dict[frozenset[str], frozenset[int]] = {}
    for continuation in continuations.values():
        requested[continuation.excluded] = requested.get(continuation.excluded, frozenset()) | {
            continuation.slot_mask
        }
    inner_scores: dict[_Continuation, list[float]] = {
        key: [] for key in set(continuations.values())
    }
    for scenario in postgame_scenarios:
        tables = topology.best_scores_by_excluded_players(dict(scenario.values), requested)
        for inner_key, scores in inner_scores.items():
            scores.append(tables[inner_key.excluded][inner_key.slot_mask])
    means = {key: sum(scores) / len(scores) for key, scores in inner_scores.items()}
    flexibility = {
        child: future_player_sets(
            future_inputs, open_slots, excluded=child.excluded, slot_mask=child.slot_mask
        )
        for child in inner_scores
    }
    thresholds: dict[tuple[_Continuation, _Continuation], float] = {}
    choices: dict[tuple[_Placement, ...], tuple[_Choice, ...]] = {}
    for key, subsets_for_assignment in subsets.items():
        prepared = []
        for subset in subsets_for_assignment:
            child = continuations[subset]
            gates = []
            for item in subset:
                if item.player_id not in topology.player_ids:
                    continue
                parent = continuations[tuple(other for other in subset if other != item)]
                edge = (parent, child)
                if edge not in thresholds:
                    differences = sorted(
                        a - b
                        for a, b in zip(inner_scores[parent], inner_scores[child], strict=True)
                    )
                    required_wins = ceil(minimum_confidence * len(differences))
                    confidence_floor = (
                        differences[required_wins - 1] if required_wins else float("-inf")
                    )
                    thresholds[edge] = (
                        max(means[parent] - means[child], confidence_floor) + tie_tolerance
                    )
                gates.append((item.sample_id, thresholds[edge]))
            prepared.append(
                _Choice(
                    tuple(item.sample_id for item in subset),
                    child,
                    means[child],
                    tuple(gates),
                    flexibility[child],
                )
            )
        choices[key] = tuple(prepared)
    fixed_score = sum(item.score for item in fixed_assignments)
    totals = {key: 0.0 for key in choices}
    samples: dict[tuple[_Placement, ...], list[float]] = {key: [] for key in choices}
    for scenario in scenarios:
        sampled = dict(scenario.values)
        tables = topology.best_scores_by_excluded_players(sampled, requested)
        for key, choices_for_assignment in choices.items():
            best = choices_for_assignment[0]
            best_score = best.expected_future
            retained_score = 0.0
            for choice in choices_for_assignment[1:]:
                if any(
                    sampled.get(candidate_id, 0) <= threshold
                    for candidate_id, threshold in choice.thresholds
                ):
                    continue
                current_score = sum(
                    sampled.get(candidate_id, 0) for candidate_id in choice.sample_ids
                )
                value = current_score + choice.expected_future
                if value > best_score + tie_tolerance or (
                    best.sample_ids
                    and abs(value - best_score) <= tie_tolerance
                    and choice.flexibility
                    and best.flexibility
                    and choice.flexibility > best.flexibility
                ):
                    best, best_score, retained_score = choice, value, current_score
            continuation = best.continuation
            terminal = (
                fixed_score + retained_score + tables[continuation.excluded][continuation.slot_mask]
            )
            totals[key] += terminal
            if score_samples is not None:
                samples[key].append(terminal)
    if score_samples is not None:
        score_samples.update(
            {assignment: tuple(samples[key]) for assignment, key in assignment_keys.items()}
        )
    return {
        assignment: round(totals[key] / len(scenarios), 6)
        for assignment, key in assignment_keys.items()
    }


def _placements(
    assignment: AssignmentResult,
    candidates: dict[str, AssignmentCandidate],
    slot_bits: dict[int, int],
    ignored: frozenset[str],
) -> tuple[_Placement, ...]:
    """Omit preserved active starters from this batch's sampled postgame stage."""
    return tuple(
        _Placement(
            item.candidate_id.rsplit("@slot-", 1)[0],
            candidates[item.candidate_id].player_id,
            slot_bits[item.slot_index],
        )
        for item in assignment.assignments
        if item.candidate_id is not None and item.candidate_id not in ignored
    )
