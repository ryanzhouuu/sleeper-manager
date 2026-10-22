"""Translate complete target lineups into atomic, position-valid roster actions."""

from dataclasses import replace
from datetime import datetime, timedelta

from sleeper_manager.decisions._lineup_move_search import LineupMoveSearch
from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.domain.eligibility import eligible_for_slot
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
    """Reach the target through legal swaps, preserving active starters and locks."""
    slots = {slot.index: slot.position for slot in state.starter_slots}
    desired = {item.slot_index: item.player_id for item in desired_view}
    if len(desired) != len(desired_view) or not set(desired) <= slots.keys():
        raise WeeklyPlanError("Desired lineup references duplicate or unknown slots")
    if any(item.slot_position != slots[item.slot_index] for item in desired_view):
        raise WeeklyPlanError("Desired lineup disagrees with slot positions")
    fixed = {item.slot_index: item.player_id for item in state.fixed_slots}
    if any(desired.get(index, player) != player for index, player in fixed.items()):
        raise WeeklyPlanError("Desired lineup would move a fixed player")
    desired.update(fixed)
    if desired.keys() != slots.keys():
        raise WeeklyPlanError("Desired lineup must cover every open slot")
    players = [player for player in desired.values() if player is not None]
    if len(set(players)) != len(players) or not set(players) <= set(state.roster_player_ids):
        raise WeeklyPlanError("Desired lineup repeats a player or includes an unrostered player")
    if set(players) & set(state.reserve_player_ids):
        raise WeeklyPlanError("Desired lineup cannot start a reserve player")
    observed = {item.slot_index: item.player_id for item in state.observed_starters}
    locked_players = set(fixed.values())
    if any(observed.get(index) != player for index, player in fixed.items()):
        raise WeeklyPlanError("Observed lineup does not preserve a fixed player")
    positions = dict(state.roster_eligibility)
    positions.update({item.player_id: item.eligible_positions for item in state.observed_starters})
    for opportunity in state.opportunities:
        positions.setdefault(opportunity.sleeper_player_id, opportunity.eligible_positions)
    eligible = {
        player: frozenset(
            index
            for index, slot in slots.items()
            if eligible_for_slot(positions.get(player, ()), slot)
        )
        for player in state.roster_player_ids
    }
    if any(
        player is not None and index not in eligible[player] for index, player in desired.items()
    ):
        raise WeeklyPlanError("Desired player is not eligible for its slot")
    forfeited = set(state.recovery_forfeitures)
    active_players = {
        item.sleeper_player_id
        for item in state.active_opportunities
        if (item.sleeper_player_id, item.game_id) not in forfeited
    }
    protected = frozenset(observed.values()) & (active_players - locked_players)
    if not protected <= set(players):
        raise WeeklyPlanError("Lineup moves would bench an active starter")
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    pending_scores = {
        item.sleeper_player_id
        for item in state.completed_opportunities
        if (item.sleeper_player_id, item.game_id) not in passed | forfeited
    }
    protected |= frozenset(observed.values()) & set(players) & (pending_scores - locked_players)
    indices = tuple(sorted(slots.keys() - fixed.keys()))
    movable_roster = (
        frozenset(state.roster_player_ids) - locked_players - set(state.reserve_player_ids)
    )
    search = LineupMoveSearch(
        slot_indices=indices,
        roster=movable_roster,
        eligible_slots=eligible,
        protected=protected,
        bench_capacity=(
            state.bench_capacity
            if state.bench_capacity is not None
            else len(movable_roster) - sum(observed.get(index) is not None for index in indices)
        ),
        deadline=batch_start,
    )
    moves = search.find(tuple(observed.get(i) for i in indices), tuple(desired[i] for i in indices))
    affected = {
        player
        for move in moves
        for player in (move.player_id, move.swap_player_id)
        if player is not None
    }
    sequence_deadline = min(
        (_move_deadline(state, player, batch_start, lead_time) for player in affected),
        default=batch_start,
    )
    return tuple(replace(move, deadline=sequence_deadline) for move in moves)


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
