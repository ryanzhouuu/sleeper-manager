"""Prove future roster flexibility through feasible player sets, without counting a player twice."""

from sleeper_manager.decisions.simulation import ScenarioInput
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import StarterSlot


def future_player_sets(
    inputs: tuple[ScenarioInput, ...],
    slots: tuple[StarterSlot, ...],
    *,
    excluded: frozenset[str],
    slot_mask: int,
) -> frozenset[int]:
    """Return feasible player subsets; an empty result means the proof exceeded its budget."""
    players = sorted({item.player_id for item in inputs})
    states = {(0, 0)}
    for player_index, player in enumerate(players):
        if player in excluded:
            continue
        edges = {
            1 << i
            for i, slot in enumerate(slots)
            if slot_mask & (1 << i)
            and any(
                item.player_id == player
                and (item.eligible_slot_indices is None or slot.index in item.eligible_slot_indices)
                and eligible_for_slot(item.eligible_positions, slot.position)
                for item in inputs
            )
        }
        updated = set(states)
        for occupied, used in states:
            for bit in edges:
                if not occupied & bit:
                    updated.add((occupied | bit, used | (1 << player_index)))
                    if len(updated) > 10000:
                        return frozenset()
        states = updated
    return frozenset(used for _, used in states)
