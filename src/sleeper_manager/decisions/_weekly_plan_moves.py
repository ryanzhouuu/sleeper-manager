"""Translate a desired weekly assignment into safe lineup moves.

The planner facade supplies observed and desired slot views. This module owns
move ordering, including temporary bench steps needed to break occupied cycles,
and applies one shared deadline to every dependent step in a sequence.
"""

from datetime import datetime, timedelta

from sleeper_manager.domain.planning import (
    LineupMove,
    PlannedAssignment,
    PlanningGameStatus,
    TeamWeekState,
)


def plan_moves(
    state: TeamWeekState,
    desired_view: tuple[PlannedAssignment, ...],
    batch_start: datetime,
    lead_time: timedelta,
) -> tuple[LineupMove, ...]:
    """Order legal moves and bench steps before the earliest affected tipoff."""
    fixed_indices = {fixed.slot_index for fixed in state.fixed_slots}
    open_indices = [slot.index for slot in state.starter_slots if slot.index not in fixed_indices]
    observed_players = {
        starter.slot_index: starter.player_id for starter in state.observed_starters
    }
    desired_players = {assignment.slot_index: assignment.player_id for assignment in desired_view}

    pending: dict[str, list[int | None]] = {}

    def pending_move(player_id: str) -> list[int | None]:
        """Return the mutable source/target pair tracked for one player."""
        return pending.setdefault(player_id, [None, None])

    for index in open_indices:
        current = observed_players.get(index)
        wanted = desired_players.get(index)
        if current == wanted:
            continue
        if current is not None:
            pending_move(current)[0] = index
        if wanted is not None:
            pending_move(wanted)[1] = index

    occupied = {
        index: player for index, player in observed_players.items() if index in open_indices
    }
    # Every step must land before the earliest tipoff the sequence depends on,
    # so a swap's bench step cannot carry a later deadline than its dependent fill.
    sequence_deadline = min(
        (_move_deadline(state, player_id, batch_start, lead_time) for player_id in pending),
        default=batch_start,
    )
    emitted: list[LineupMove] = []
    while pending:
        progressed = False
        for player_id in sorted(pending):
            source, target = pending[player_id]
            if target is not None and target in occupied:
                continue
            emitted.append(
                LineupMove(
                    player_id=player_id,
                    source_slot_index=source,
                    target_slot_index=target,
                    deadline=sequence_deadline,
                )
            )
            if source is not None:
                occupied.pop(source)
            if target is not None:
                occupied[target] = player_id
            del pending[player_id]
            progressed = True
        if pending and not progressed:
            player_id = min(pending)
            source, target = pending.pop(player_id)
            emitted.append(
                LineupMove(
                    player_id=player_id,
                    source_slot_index=source,
                    target_slot_index=None,
                    deadline=sequence_deadline,
                )
            )
            if source is not None:
                occupied.pop(source)
            pending[player_id] = [None, target]
    return tuple(emitted)


def _move_deadline(
    state: TeamWeekState,
    player_id: str,
    batch_start: datetime,
    lead_time: timedelta,
) -> datetime:
    """Return the first unpassed future tipoff minus the configured lead time."""
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    starts = sorted(
        opportunity.scheduled_start
        for opportunity in state.opportunities
        if opportunity.sleeper_player_id == player_id
        and opportunity.status is PlanningGameStatus.SCHEDULED
        and opportunity.scheduled_start > state.decision_time
        and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
    )
    earliest = starts[0] if starts else batch_start
    return earliest - lead_time
