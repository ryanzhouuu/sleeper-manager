"""Restrict accepted-score placements to lineups that retain every active starter."""

from sleeper_manager.decisions.completed_scores import executable_lock_placements
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import GameOpportunity, TeamWeekState


def feasible_lock_slot_indices(state: TeamWeekState, game: GameOpportunity) -> frozenset[int]:
    """Allow active starter realignment, while preserving fixed slots and tipoff eligibility."""
    if game.sleeper_player_id in state.reserve_player_ids:
        return frozenset()
    if any(row.player_id == game.sleeper_player_id for row in state.observed_starters):
        return frozenset(
            lock.slot_index
            for target in executable_lock_placements(state, (game,))
            for lock in target.locks
        )
    slots = tuple(slot for slot in state.starter_slots if slot.index in state.open_slot_indices)
    active_by_player = {item.sleeper_player_id: item for item in state.active_opportunities}
    active = tuple(
        AssignmentCandidate(
            f"active:{starter.player_id}",
            starter.player_id,
            1,
            starter.eligible_positions,
            eligible_slot_indices=active_by_player[starter.player_id].eligible_slot_indices,
        )
        for starter in state.observed_starters
        if starter.player_id in active_by_player
        and starter.player_id != game.sleeper_player_id
        and starter.player_id not in {fixed.player_id for fixed in state.fixed_slots}
    )
    target = AssignmentCandidate(
        "lock",
        game.sleeper_player_id,
        1,
        game.eligible_positions,
        eligible_slot_indices=game.eligible_slot_indices,
    )
    feasible = []
    for slot in slots:
        if slot.index not in game.eligible_slot_indices or not eligible_for_slot(
            game.eligible_positions, slot.position
        ):
            continue
        result = maximum_weight_assignment(
            (*active, target),
            tuple(item.position for item in slots),
            slot_indices=tuple(item.index for item in slots),
            required_edges=frozenset({(slot.index, "lock")}),
        )
        if result.score == len(active) + 1:
            feasible.append(slot.index)
    return frozenset(feasible)
