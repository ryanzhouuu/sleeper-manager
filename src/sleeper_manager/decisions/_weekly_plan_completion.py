"""Complete batch placements without scoring or silently removing later starters."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    SlotAssignment,
    maximum_weight_assignment,
)
from sleeper_manager.domain.planning import GameOpportunity, PlannedAssignment, TeamWeekState


def executable_completions(
    state: TeamWeekState,
    batch: tuple[GameOpportunity, ...],
    assignments: tuple[AssignmentResult, ...],
) -> dict[AssignmentResult, tuple[SlotAssignment, ...]]:
    """Exclude options whose completed targets cannot preserve active eligibility."""
    completed = {}
    for assignment in assignments:
        try:
            target = complete_assignment(state, batch, assignment)
            plan_moves(
                state,
                tuple(
                    PlannedAssignment(item.slot_index, item.slot_position, item.player_id)
                    for item in target
                ),
                batch[0].scheduled_start,
                timedelta(0),
            )
        except WeeklyPlanError:
            continue
        completed[assignment] = target
    if not completed:
        raise WeeklyPlanError("No safe complete lineup remains")
    return completed


def complete_assignment(
    state: TeamWeekState,
    batch: tuple[GameOpportunity, ...],
    assignment: AssignmentResult,
) -> tuple[SlotAssignment, ...]:
    """Keep batch placements, then retain as many other observed starters as fit.

    Retention matching uses cardinality, not fantasy points. Returned retained
    players have zero score and no game, and must never enter batch simulation.
    Fixed slots remain the caller's responsibility. Raises WeeklyPlanError if
    completion would omit a protected active starter.
    """
    slots = tuple(
        sorted(
            (slot for slot in state.starter_slots if slot.index in state.open_slot_indices),
            key=lambda slot: slot.index,
        )
    )
    placed = {item.slot_index: item for item in assignment.assignments if item.player_id}
    placed_players = {item.player_id for item in placed.values()}
    batch_players = {item.sleeper_player_id for item in batch}
    retained = tuple(
        starter
        for starter in state.observed_starters
        if starter.slot_index in state.open_slot_indices
        and starter.player_id not in placed_players | batch_players
    )
    remaining = tuple(slot for slot in slots if slot.index not in placed)
    if all(starter.slot_index not in placed for starter in retained):
        by_slot = {starter.slot_index: starter for starter in retained}
        filled = tuple(
            SlotAssignment(
                slot.index,
                slot.position,
                f"retained:{by_slot[slot.index].player_id}" if slot.index in by_slot else None,
                by_slot[slot.index].player_id if slot.index in by_slot else None,
                None,
                0.0,
            )
            for slot in remaining
        )
    else:
        observed = {starter.slot_index: starter.player_id for starter in retained}

        def tie_key(items: tuple[SlotAssignment, ...]) -> tuple[object, ...]:
            """Prefer unchanged slots, then stable slot/player identities."""
            return (
                sum(item.player_id != observed.get(item.slot_index) for item in items),
                tuple((item.slot_index, item.player_id or "") for item in items),
            )

        matched = maximum_weight_assignment(
            tuple(
                AssignmentCandidate(
                    f"retained:{starter.player_id}",
                    starter.player_id,
                    1.0,
                    starter.eligible_positions,
                )
                for starter in retained
            ),
            tuple(slot.position for slot in remaining),
            slot_indices=tuple(slot.index for slot in remaining),
            tie_break_key=tie_key,
        )
        filled = tuple(replace(item, score=0.0) for item in matched.assignments)
    completed = tuple(sorted((*placed.values(), *filled), key=lambda item: item.slot_index))
    active_players = {
        item.sleeper_player_id
        for item in state.active_opportunities
        if (item.sleeper_player_id, item.game_id) not in state.recovery_forfeitures
    }
    protected = {
        starter.player_id
        for starter in state.observed_starters
        if starter.slot_index in state.open_slot_indices and starter.player_id in active_players
    }
    if not protected <= {item.player_id for item in completed}:
        raise WeeklyPlanError("Lineup completion would omit an active starter")
    return completed
