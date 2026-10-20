"""Lineup action contracts and ordered occupancy checks used by weekly plans."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.domain._planning_validation import (
    PlanningStateError,
    _require_aware,
    _require_text,
)


@dataclass(frozen=True, slots=True)
class LineupMove:
    player_id: str
    source_slot_index: int | None
    target_slot_index: int | None
    deadline: datetime

    def __post_init__(self) -> None:
        _require_text(self.player_id, "Lineup move player ID")
        if self.source_slot_index is None and self.target_slot_index is None:
            raise PlanningStateError("A lineup move must change a slot or the bench")
        if self.source_slot_index == self.target_slot_index:
            raise PlanningStateError("A lineup move cannot reference the same slot twice")
        for label, index in (
            ("Lineup move source", self.source_slot_index),
            ("Lineup move target", self.target_slot_index),
        ):
            if index is not None and index < 0:
                raise PlanningStateError(f"{label} slot indices must be non-negative")
        _require_aware(self.deadline, "Lineup move deadline")


def apply_lineup_moves(
    moves: tuple[LineupMove, ...],
    observed_players: Mapping[int, str | None],
) -> dict[int, str | None]:
    """Replay ordered actions, rejecting stale sources and occupied destinations."""
    simulated = dict(observed_players)
    for move in moves:
        if move.source_slot_index is not None:
            if simulated.get(move.source_slot_index) != move.player_id:
                raise PlanningStateError("A lineup move source does not hold its player")
            simulated[move.source_slot_index] = None
        elif move.player_id in set(simulated.values()):
            raise PlanningStateError("A bench-source move requires a benched player")
        if move.target_slot_index is not None:
            if simulated.get(move.target_slot_index) is not None:
                raise PlanningStateError("A lineup move targets an occupied slot")
            simulated[move.target_slot_index] = move.player_id
    return simulated
