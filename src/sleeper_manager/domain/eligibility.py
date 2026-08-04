from collections.abc import Iterable

FLEX_POSITIONS: dict[str, frozenset[str]] = {
    "G": frozenset({"PG", "SG"}),
    "F": frozenset({"SF", "PF"}),
    "UTIL": frozenset({"PG", "SG", "SF", "PF", "C"}),
}


def eligible_for_slot(player_positions: Iterable[str], slot: str) -> bool:
    positions = frozenset(player_positions)
    return slot in positions or bool(positions & FLEX_POSITIONS.get(slot, frozenset()))
