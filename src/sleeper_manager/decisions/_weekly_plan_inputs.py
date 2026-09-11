"""Select and normalize the opportunities evaluated by the weekly planner.

This module defines the boundary between a validated ``TeamWeekState`` and the
assignment/scenario records consumed by weekly-plan scoring.
"""

from collections.abc import Iterable
from datetime import datetime

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions.lineup import AssignmentCandidate
from sleeper_manager.decisions.simulation import ScenarioInput
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    PlanningGameStatus,
    StarterSlot,
    TeamWeekState,
)


def option_candidates(
    batch: tuple[GameOpportunity, ...],
    open_slots: tuple[StarterSlot, ...],
) -> tuple[AssignmentCandidate, ...]:
    """Expand each batch opportunity into one candidate per eligible open slot."""
    candidates: list[AssignmentCandidate] = []
    for opportunity in batch:
        assert opportunity.projection is not None
        for slot in open_slots:
            slot_index = slot.index
            if slot_index not in opportunity.eligible_slot_indices:
                continue
            candidate_id = f"{opportunity_id(opportunity)}@slot-{slot_index}"
            candidates.append(
                AssignmentCandidate(
                    candidate_id=candidate_id,
                    player_id=opportunity.sleeper_player_id,
                    score=opportunity.projection.distribution.expected_value,
                    eligible_positions=opportunity.eligible_positions,
                    game_id=opportunity.game_id,
                    eligible_slot_indices=(slot_index,),
                )
            )
    return tuple(candidates)


def active_assignment_candidates(
    state: TeamWeekState,
    open_slots: tuple[StarterSlot, ...],
) -> tuple[AssignmentCandidate, ...]:
    """Represent observed in-progress starters as required zero-value placements."""

    active_by_player: dict[str, list[GameOpportunity]] = {}
    for opportunity in state.active_opportunities:
        active_by_player.setdefault(opportunity.sleeper_player_id, []).append(opportunity)
    candidates: list[AssignmentCandidate] = []
    for starter in sorted(state.observed_starters, key=lambda item: item.player_id):
        active = active_by_player.get(starter.player_id, [])
        if not active:
            continue
        if len(active) > 1:
            raise WeeklyPlanError(
                f"Multiple active opportunities for observed starter {starter.player_id}"
            )
        opportunity = active[0]
        eligible_indices = tuple(
            slot.index for slot in open_slots if slot.index in opportunity.eligible_slot_indices
        )
        if not eligible_indices:
            raise WeeklyPlanError(f"Active starter has no eligible open slot: {starter.player_id}")
        candidates.append(
            AssignmentCandidate(
                candidate_id=f"active:{opportunity_id(opportunity)}",
                player_id=starter.player_id,
                score=0.0,
                eligible_positions=starter.eligible_positions,
                game_id=opportunity.game_id,
                eligible_slot_indices=eligible_indices,
            )
        )
    return tuple(candidates)


def next_actionable_batch(state: TeamWeekState) -> tuple[GameOpportunity, ...] | None:
    """Return the earliest unpassed scheduled opportunities sharing a tipoff."""
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    locked_players = {item.player_id for item in state.fixed_slots}
    candidates = tuple(
        opportunity
        for opportunity in state.opportunities
        if opportunity.sleeper_player_id not in locked_players
        if opportunity.status is PlanningGameStatus.SCHEDULED
        and opportunity.scheduled_start > state.decision_time
        and opportunity.rostered_at_tipoff is True
        and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
    )
    if not candidates:
        return None
    batch_start = min(opportunity.scheduled_start for opportunity in candidates)
    return tuple(
        sorted(
            (
                opportunity
                for opportunity in candidates
                if opportunity.scheduled_start == batch_start
            ),
            key=opportunity_id,
        )
    )


def future_opportunities(
    state: TeamWeekState, batch_start: datetime
) -> tuple[GameOpportunity, ...]:
    """Return later rostered opportunities still eligible for replanning."""
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    locked_players = {item.player_id for item in state.fixed_slots}
    return tuple(
        sorted(
            (
                opportunity
                for opportunity in state.opportunities
                if opportunity.sleeper_player_id not in locked_players
                if opportunity.scheduled_start > batch_start
                and opportunity.status in (PlanningGameStatus.SCHEDULED, PlanningGameStatus.ACTIVE)
                and opportunity.rostered_at_tipoff is True
                and (opportunity.sleeper_player_id, opportunity.game_id) not in passed
            ),
            key=opportunity_id,
        )
    )


def scenario_input(opportunity: GameOpportunity) -> ScenarioInput:
    """Normalize a projected opportunity or fail closed when its projection is missing."""
    if opportunity.projection is None:
        raise WeeklyPlanError(
            f"Missing projection for {opportunity.sleeper_player_id}:{opportunity.game_id}"
        )
    return ScenarioInput(
        candidate_id=opportunity_id(opportunity),
        player_id=opportunity.sleeper_player_id,
        game_id=opportunity.game_id,
        eligible_positions=opportunity.eligible_positions,
        projection=opportunity.projection,
        eligible_slot_indices=opportunity.eligible_slot_indices,
    )


def fixed_assignments(fixed_slots: Iterable[FixedSlot]) -> tuple[AssignmentCandidate, ...]:
    """Represent accepted fixed slots as immutable assignment candidates."""
    return tuple(
        AssignmentCandidate(
            candidate_id=f"fixed:{fixed.slot_index}:{fixed.player_id}:{fixed.game_id}",
            player_id=fixed.player_id,
            score=fixed.accepted_fantasy_score,
            eligible_positions=(fixed.slot_position,),
            game_id=fixed.game_id,
            eligible_slot_indices=(fixed.slot_index,),
        )
        for fixed in fixed_slots
    )


def opportunity_id(opportunity: GameOpportunity) -> str:
    """Build the stable player/game/membership identity used by scenarios."""
    return (
        f"{opportunity.sleeper_player_id}:{opportunity.game_id}:"
        f"{opportunity.membership_segment or opportunity.roster_id}"
    )
