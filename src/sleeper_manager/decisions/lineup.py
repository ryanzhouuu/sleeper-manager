from collections.abc import Callable
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
    tie_break_key: Callable[[tuple[SlotAssignment, ...]], tuple[object, ...]] | None = None,
    tie_tolerance: float = 1e-9,
) -> AssignmentResult:
    """Solve a small maximum-weight player/slot assignment without external solvers."""

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
    player_ids = {candidate.player_id for candidate in candidate_records}
    player_bits = {player_id: 1 << index for index, player_id in enumerate(sorted(player_ids))}

    @cache
    def solve(slot_index: int, used_players: int) -> AssignmentResult | None:
        if slot_index == len(slot_records):
            return AssignmentResult(0.0, ())
        best: AssignmentResult | None = None
        if slot_index_records[slot_index] not in required_by_slot:
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
        raise ValueError("Required assignment edges cannot be satisfied together")
    return AssignmentResult(round(result.score, 6), result.assignments)


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
