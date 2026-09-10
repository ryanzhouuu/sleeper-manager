"""Deterministic weighted lineup assignment under player and slot constraints."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from math import isfinite
from typing import NamedTuple


@dataclass(frozen=True, slots=True)
class LineupAssignment:
    slot: str
    player_id: str
    projected_points: float


@dataclass(frozen=True, slots=True)
class AssignmentCandidate:
    candidate_id: str
    player_id: str
    score: float
    eligible_positions: tuple[str, ...]
    game_id: str | None = None
    eligible_slot_indices: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.player_id.strip():
            raise ValueError("Assignment candidates require stable IDs")
        if not isfinite(self.score):
            raise ValueError("Assignment candidate scores must be finite")
        if self.eligible_slot_indices is not None:
            if len(set(self.eligible_slot_indices)) != len(self.eligible_slot_indices):
                raise ValueError("Candidate slot indices must be unique")
            if any(index < 0 for index in self.eligible_slot_indices):
                raise ValueError("Candidate slot indices must be non-negative")


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    slot_index: int
    slot_position: str
    candidate_id: str | None
    player_id: str | None
    game_id: str | None
    score: float


class AssignmentResult(NamedTuple):
    score: float
    assignments: tuple[SlotAssignment, ...]


def maximum_weight_assignment(
    candidates: tuple[AssignmentCandidate, ...] | list[AssignmentCandidate],
    slots: tuple[str, ...] | list[str],
    *,
    slot_indices: tuple[int, ...] | list[int] | None = None,
    forbidden_edges: frozenset[tuple[int, str]] = frozenset(),
    required_edges: frozenset[tuple[int, str]] = frozenset(),
    require_full_cardinality: bool = False,
    tie_break_key: Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]] | None = None,
    tie_tolerance: float = 1e-9,
) -> AssignmentResult:
    """Solve a small assignment, optionally prohibiting empty starter slots."""

    candidate_records = tuple(candidates)
    slot_records = tuple(slot.upper() for slot in slots)
    if slot_indices is None:
        slot_index_records = tuple(range(len(slot_records)))
    else:
        slot_index_records = tuple(slot_indices)
        if len(slot_index_records) != len(slot_records):
            raise ValueError("Slot indices must match the number of slots")
        if len(set(slot_index_records)) != len(slot_index_records):
            raise ValueError("Slot indices must be unique")
        if any(index < 0 for index in slot_index_records):
            raise ValueError("Slot indices must be non-negative")
    if not isfinite(tie_tolerance) or tie_tolerance < 0:
        raise ValueError("Assignment tie tolerance must be finite and non-negative")
    required_by_slot = dict(required_edges)
    if len(required_by_slot) != len(required_edges):
        raise ValueError("Required assignment edges cannot reuse a slot")
    if required_edges & forbidden_edges:
        raise ValueError("An assignment edge cannot be required and forbidden")
    if not slot_records:
        return AssignmentResult(0.0, ())
    by_slot: tuple[tuple[AssignmentCandidate, ...], ...] = tuple(
        tuple(
            sorted(
                (
                    candidate
                    for candidate in candidate_records
                    if candidate_eligible_for_slot(
                        candidate,
                        slot_index=slot_index_records[index],
                        slot_position=slot,
                    )
                    and (
                        slot_index_records[index] not in required_by_slot
                        or required_by_slot[slot_index_records[index]] == candidate.candidate_id
                    )
                    and (slot_index_records[index], candidate.candidate_id) not in forbidden_edges
                ),
                key=lambda candidate: candidate.candidate_id,
            )
        )
        for index, slot in enumerate(slot_records)
    )
    for index, slot_index in enumerate(slot_index_records):
        if slot_index in required_by_slot and not by_slot[index]:
            raise ValueError(f"Required assignment edge is not feasible for slot {slot_index}")
    required_players = {
        candidate.player_id
        for slot_index, candidate_id in required_edges
        for candidate in candidate_records
        if candidate.candidate_id == candidate_id and slot_index in required_by_slot
    }
    if len(required_players) != len(required_edges):
        raise ValueError("Required assignment edges cannot reuse a player")
    if tie_break_key is None:
        return _assignment_by_slot_mask(
            by_slot,
            slot_records,
            slot_index_records,
            required_by_slot,
            required_players,
            require_full_cardinality,
            tie_tolerance,
        )
    player_ids = {candidate.player_id for candidate in candidate_records}
    player_bits = {player_id: 1 << index for index, player_id in enumerate(sorted(player_ids))}

    @cache
    def solve(slot_index: int, used_players: int) -> AssignmentResult | None:
        if slot_index == len(slot_records):
            return AssignmentResult(0.0, ())
        best: AssignmentResult | None = None
        if slot_index_records[slot_index] not in required_by_slot and not require_full_cardinality:
            remainder = solve(slot_index + 1, used_players)
            if remainder is not None:
                best = AssignmentResult(
                    remainder.score,
                    (
                        SlotAssignment(
                            slot_index_records[slot_index],
                            slot_records[slot_index],
                            None,
                            None,
                            None,
                            0.0,
                        ),
                    )
                    + remainder.assignments,
                )
        for candidate in by_slot[slot_index]:
            bit = player_bits[candidate.player_id]
            if used_players & bit:
                continue
            remainder = solve(slot_index + 1, used_players | bit)
            if remainder is None:
                continue
            current = AssignmentResult(
                candidate.score + remainder.score,
                (
                    SlotAssignment(
                        slot_index_records[slot_index],
                        slot_records[slot_index],
                        candidate.candidate_id,
                        candidate.player_id,
                        candidate.game_id,
                        candidate.score,
                    ),
                )
                + remainder.assignments,
            )
            if best is None or _better_assignment(current, best, tie_break_key, tie_tolerance):
                best = current
        return best

    result = solve(0, 0)
    if result is None:
        if require_full_cardinality:
            raise ValueError("Full-cardinality assignment is infeasible")
        raise ValueError("Required assignment edges cannot be satisfied together")
    return AssignmentResult(round(result.score, 6), result.assignments)


def _assignment_by_slot_mask(
    by_slot: tuple[tuple[AssignmentCandidate, ...], ...],
    slots: tuple[str, ...],
    slot_indices: tuple[int, ...],
    required_by_slot: Mapping[int, str],
    required_players: set[str],
    require_full_cardinality: bool,
    tie_tolerance: float,
) -> AssignmentResult:
    """Solve the normal tie policy in O(players × edges × 2^slots) states."""

    edges_by_player: dict[str, list[tuple[int, AssignmentCandidate]]] = {}
    for position, candidates in enumerate(by_slot):
        for candidate in candidates:
            edges_by_player.setdefault(candidate.player_id, []).append((position, candidate))
    required_edge_by_player = {
        candidate.player_id: (position, candidate.candidate_id)
        for position, candidates in enumerate(by_slot)
        for candidate in candidates
        if required_by_slot.get(slot_indices[position]) == candidate.candidate_id
    }
    candidate_records = tuple(
        sorted(
            {candidate for candidates in by_slot for candidate in candidates},
            key=lambda item: (
                item.candidate_id,
                item.player_id,
                item.game_id or "",
                item.score,
            ),
        )
    )
    candidate_codes = {candidate: index + 1 for index, candidate in enumerate(candidate_records)}
    candidate_by_code = {index + 1: candidate for index, candidate in enumerate(candidate_records)}
    selection_base = len(candidate_records) + 1
    selection_powers = tuple(selection_base**position for position in range(len(slots)))
    candidate_ids = tuple(sorted({candidate.candidate_id for candidate in candidate_records}))
    candidate_ranks = {candidate_id: index + 1 for index, candidate_id in enumerate(candidate_ids)}
    lexicographic_base = len(candidate_ids) + 1
    lexicographic_powers = tuple(
        lexicographic_base ** (len(slots) - position - 1) for position in range(len(slots))
    )
    state_count = 1 << len(slots)
    scores = [float("-inf")] * state_count
    selection_codes = [0] * state_count
    lexicographic_codes = [0] * state_count
    scores[0] = 0.0
    for player_id in sorted(edges_by_player):
        player_edges = tuple(
            sorted(edges_by_player[player_id], key=lambda item: (item[0], item[1].candidate_id))
        )
        required_edge = required_edge_by_player.get(player_id)
        if player_id in required_players:
            next_scores = [float("-inf")] * state_count
            next_selection_codes = [0] * state_count
            next_lexicographic_codes = [0] * state_count
        else:
            next_scores = scores.copy()
            next_selection_codes = selection_codes.copy()
            next_lexicographic_codes = lexicographic_codes.copy()
        for mask, base_score in enumerate(scores):
            if base_score == float("-inf"):
                continue
            for position, candidate in player_edges:
                if mask & (1 << position):
                    continue
                if (
                    required_edge is not None
                    and (
                        position,
                        candidate.candidate_id,
                    )
                    != required_edge
                ):
                    continue
                candidate_score = base_score + candidate.score
                candidate_mask = mask | (1 << position)
                candidate_lexicographic_code = (
                    lexicographic_codes[mask]
                    + candidate_ranks[candidate.candidate_id] * lexicographic_powers[position]
                )
                incumbent_score = next_scores[candidate_mask]
                if candidate_score > incumbent_score + tie_tolerance or (
                    abs(candidate_score - incumbent_score) <= tie_tolerance
                    and candidate_lexicographic_code < next_lexicographic_codes[candidate_mask]
                ):
                    next_scores[candidate_mask] = candidate_score
                    next_selection_codes[candidate_mask] = (
                        selection_codes[mask]
                        + candidate_codes[candidate] * selection_powers[position]
                    )
                    next_lexicographic_codes[candidate_mask] = candidate_lexicographic_code
        scores = next_scores
        selection_codes = next_selection_codes
        lexicographic_codes = next_lexicographic_codes
    required_mask = sum(
        1 << position
        for position, slot_index in enumerate(slot_indices)
        if slot_index in required_by_slot
    )
    full_mask = (1 << len(slots)) - 1
    best_mask: int | None = None
    best_score = float("-inf")
    best_lexicographic_code = 0
    for mask, score in enumerate(scores):
        if score == float("-inf"):
            continue
        if mask & required_mask != required_mask:
            continue
        if require_full_cardinality and mask != full_mask:
            continue
        lexicographic_code = lexicographic_codes[mask]
        if score > best_score + tie_tolerance or (
            abs(score - best_score) <= tie_tolerance
            and (best_mask is None or lexicographic_code < best_lexicographic_code)
        ):
            best_mask = mask
            best_score = score
            best_lexicographic_code = lexicographic_code
    if best_mask is None:
        if require_full_cardinality:
            raise ValueError("Full-cardinality assignment is infeasible")
        raise ValueError("Required assignment edges cannot be satisfied together")
    selection_code = selection_codes[best_mask]
    selected = tuple(
        candidate_by_code.get((selection_code // selection_powers[position]) % selection_base)
        for position in range(len(slots))
    )
    assignments = tuple(
        SlotAssignment(
            slot_indices[position],
            slots[position],
            candidate.candidate_id if candidate is not None else None,
            candidate.player_id if candidate is not None else None,
            candidate.game_id if candidate is not None else None,
            candidate.score if candidate is not None else 0.0,
        )
        for position, candidate in enumerate(selected)
    )
    return AssignmentResult(round(best_score, 6), assignments)


def candidate_eligible_for_slot(
    candidate: AssignmentCandidate,
    *,
    slot_index: int,
    slot_position: str,
) -> bool:
    return _eligible(candidate.eligible_positions, slot_position.upper()) and _eligible_for_index(
        candidate, slot_index
    )


def _eligible(positions: tuple[str, ...], slot: str) -> bool:
    from sleeper_manager.domain.eligibility import eligible_for_slot

    return eligible_for_slot(positions, slot)


def _better_assignment(
    candidate: AssignmentResult,
    incumbent: AssignmentResult,
    tie_break_key: Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]] | None,
    tie_tolerance: float,
) -> bool:
    if candidate.score > incumbent.score + tie_tolerance:
        return True
    if abs(candidate.score - incumbent.score) > tie_tolerance:
        return False
    if tie_break_key is not None:
        return tie_break_key(candidate.assignments) < tie_break_key(incumbent.assignments)
    candidate_key = tuple(assignment.candidate_id or "" for assignment in candidate.assignments)
    incumbent_key = tuple(assignment.candidate_id or "" for assignment in incumbent.assignments)
    return candidate_key < incumbent_key


def _eligible_for_index(candidate: AssignmentCandidate, slot_index: int) -> bool:
    return candidate.eligible_slot_indices is None or slot_index in candidate.eligible_slot_indices


def projected_lineup_total(assignments: tuple[LineupAssignment, ...]) -> float:
    return round(sum(assignment.projected_points for assignment in assignments), 2)
