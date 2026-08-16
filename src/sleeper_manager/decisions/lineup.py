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

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.player_id.strip():
            raise ValueError("Assignment candidates require stable IDs")
        if not isfinite(self.score):
            raise ValueError("Assignment candidate scores must be finite")


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
) -> AssignmentResult:
    """Solve a small maximum-weight player/slot assignment without external solvers."""

    candidate_records = tuple(candidates)
    slot_records = tuple(slot.upper() for slot in slots)
    if not slot_records:
        return AssignmentResult(0.0, ())
    by_slot: tuple[tuple[AssignmentCandidate, ...], ...] = tuple(
        tuple(
            sorted(
                (
                    candidate
                    for candidate in candidate_records
                    if _eligible(candidate.eligible_positions, slot)
                ),
                key=lambda candidate: candidate.candidate_id,
            )
        )
        for slot in slot_records
    )
    player_ids = {candidate.player_id for candidate in candidate_records}
    player_bits = {player_id: 1 << index for index, player_id in enumerate(sorted(player_ids))}

    @cache
    def solve(slot_index: int, used_players: int) -> AssignmentResult:
        if slot_index == len(slot_records):
            return AssignmentResult(0.0, ())
        best = solve(slot_index + 1, used_players)
        best = AssignmentResult(
            best.score,
            (
                SlotAssignment(
                    slot_index,
                    slot_records[slot_index],
                    None,
                    None,
                    None,
                    0.0,
                ),
            )
            + best.assignments,
        )
        for candidate in by_slot[slot_index]:
            bit = player_bits[candidate.player_id]
            if used_players & bit:
                continue
            remainder = solve(slot_index + 1, used_players | bit)
            current = AssignmentResult(
                candidate.score + remainder.score,
                (
                    SlotAssignment(
                        slot_index,
                        slot_records[slot_index],
                        candidate.candidate_id,
                        candidate.player_id,
                        candidate.game_id,
                        candidate.score,
                    ),
                )
                + remainder.assignments,
            )
            if _better_assignment(current, best):
                best = current
        return best

    result = solve(0, 0)
    return AssignmentResult(round(result.score, 6), result.assignments)


def _eligible(positions: tuple[str, ...], slot: str) -> bool:
    from sleeper_manager.domain.eligibility import eligible_for_slot

    return eligible_for_slot(positions, slot)


def _better_assignment(candidate: AssignmentResult, incumbent: AssignmentResult) -> bool:
    if candidate.score > incumbent.score + 1e-9:
        return True
    if abs(candidate.score - incumbent.score) > 1e-9:
        return False
    candidate_key = tuple(assignment.candidate_id or "" for assignment in candidate.assignments)
    incumbent_key = tuple(assignment.candidate_id or "" for assignment in incumbent.assignments)
    return candidate_key < incumbent_key


def projected_lineup_total(assignments: tuple[LineupAssignment, ...]) -> float:
    return round(sum(assignment.projected_points for assignment in assignments), 2)
