"""Build and validate the constrained historical Lock-In oracle from planning state."""

from __future__ import annotations

from datetime import UTC, datetime

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticError,
    OracleFeasibilityCheck,
)
from sleeper_manager.backtesting.replay.models import TeamWeekReplayResult
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment
from sleeper_manager.domain.lock_in import LockInDecision, LockInDecisionKind
from sleeper_manager.domain.planning import FixedSlot, GameOpportunity, TeamWeekState

_ORACLE_INFORMATION_VERSION = "realized-outcomes"
_ORACLE_REASON = "Constrained maximum-weight realized assignment."


def oracle_from_planning_state(state: TeamWeekState) -> TeamWeekReplayResult:
    """Maximize realized score under the same starter slots and eligibility as the model.

    Raises LockInDiagnosticError when the completed opportunities cannot fill every starter slot.
    """

    candidates = tuple(
        AssignmentCandidate(
            candidate_id=f"{opportunity.sleeper_player_id}:{opportunity.game_id}",
            player_id=opportunity.sleeper_player_id,
            score=_realized_score(opportunity),
            eligible_positions=opportunity.eligible_positions,
            game_id=opportunity.game_id,
            eligible_slot_indices=opportunity.eligible_slot_indices,
        )
        for opportunity in state.opportunities
        if opportunity.rostered_at_tipoff is True
        if opportunity.completed_fantasy_score is not None
    )
    slot_indices = tuple(slot.index for slot in state.starter_slots)
    try:
        assignment = maximum_weight_assignment(
            candidates,
            tuple(slot.position for slot in state.starter_slots),
            slot_indices=slot_indices,
            require_full_cardinality=True,
        )
    except ValueError as error:
        raise LockInDiagnosticError(
            f"Constrained oracle assignment is infeasible: {error}"
        ) from error
    opportunity_by_key = {
        (opportunity.sleeper_player_id, opportunity.game_id): opportunity
        for opportunity in state.opportunities
    }
    locked: list[FixedSlot] = []
    automatic: list[tuple[str, float]] = []
    decisions: list[LockInDecision] = []
    selected_players = {
        item.player_id for item in assignment.assignments if item.player_id is not None
    }
    for item in assignment.assignments:
        if item.player_id is None or item.game_id is None:
            continue
        opportunity = opportunity_by_key[(item.player_id, item.game_id)]
        locked_at = opportunity.finalized_at or datetime.min.replace(tzinfo=UTC)
        if _is_final_rostered_opportunity(opportunity, state.opportunities):
            automatic.append((item.player_id, item.score))
        else:
            locked.append(
                FixedSlot(
                    slot_index=item.slot_index,
                    slot_position=item.slot_position,
                    player_id=item.player_id,
                    game_id=item.game_id,
                    accepted_fantasy_score=item.score,
                    decision_time=locked_at,
                    decision_id=f"oracle:{item.candidate_id}:{item.slot_index}",
                    provenance=_ORACLE_INFORMATION_VERSION,
                )
            )
        decisions.append(
            LockInDecision(
                decision_time=locked_at,
                kind=LockInDecisionKind.LOCK,
                player_id=item.player_id,
                game_id=item.game_id,
                slot_index=item.slot_index,
                information_version=_ORACLE_INFORMATION_VERSION,
                expected_terminal_score=item.score,
                counterfactual_value=item.score,
                reason=_ORACLE_REASON,
            )
        )
    roster_players = sorted({opportunity.sleeper_player_id for opportunity in state.opportunities})
    automatic.extend(
        (player_id, _automatic_final_score(player_id, state.opportunities))
        for player_id in roster_players
        if player_id not in selected_players
    )
    return TeamWeekReplayResult(
        league_id=state.league_id,
        week=state.week,
        roster_id=state.roster_id,
        policy_name="oracle",
        realized_score=round(assignment.score, 6),
        decisions=tuple(decisions),
        locked_slots=tuple(locked),
        automatic_final_scores=tuple(automatic),
        eligibility_quality=state.eligibility_quality.value,
        data_quality="complete" if state.eligibility_quality.value == "exact" else "partial",
    )


def oracle_feasibility_checks(
    oracle: TeamWeekReplayResult,
    state: TeamWeekState,
) -> tuple[OracleFeasibilityCheck, ...]:
    """Check whether each oracle selection could have been locked in time."""

    opportunity_by_key = {
        (opportunity.sleeper_player_id, opportunity.game_id): opportunity
        for opportunity in state.opportunities
    }
    checks: list[OracleFeasibilityCheck] = []
    for decision in oracle.decisions:
        if decision.game_id is None or decision.slot_index is None:
            continue
        opportunity = opportunity_by_key[(decision.player_id, decision.game_id)]
        if _is_final_rostered_opportunity(opportunity, state.opportunities):
            checks.append(
                OracleFeasibilityCheck(
                    sleeper_id=decision.player_id,
                    game_id=decision.game_id,
                    slot_index=decision.slot_index,
                    kind="automatic_final",
                    feasible=True,
                    detail="Selected final rostered game is treated as automatic.",
                )
            )
            continue
        finalized_at = opportunity.finalized_at
        next_start = _next_rostered_start(opportunity, state.opportunities)
        feasible = finalized_at is not None and next_start is not None and finalized_at < next_start
        checks.append(
            OracleFeasibilityCheck(
                sleeper_id=decision.player_id,
                game_id=decision.game_id,
                slot_index=decision.slot_index,
                kind="lockable_earlier",
                feasible=feasible,
                detail=(
                    f"finalized_at={None if finalized_at is None else finalized_at.isoformat()} "
                    f"next_rostered_start="
                    f"{None if next_start is None else next_start.isoformat()}"
                ),
            )
        )
    return tuple(checks)


def _realized_score(opportunity: GameOpportunity) -> float:
    """Return the realized fantasy score for one rostered completed opportunity."""

    score = opportunity.completed_fantasy_score
    if score is None:
        raise ValueError("Oracle candidates require completed fantasy scores")
    return score


def _is_final_rostered_opportunity(
    opportunity: GameOpportunity,
    opportunities: tuple[GameOpportunity, ...],
) -> bool:
    """Return whether an opportunity is the player's final rostered game."""

    return _next_rostered_start(opportunity, opportunities) is None


def _next_rostered_start(
    opportunity: GameOpportunity,
    opportunities: tuple[GameOpportunity, ...],
) -> datetime | None:
    """Return the next rostered game start for the same player, if any."""

    later = tuple(
        other.scheduled_start
        for other in opportunities
        if other.sleeper_player_id == opportunity.sleeper_player_id
        if other.rostered_at_tipoff is True
        if other.scheduled_start > opportunity.scheduled_start
    )
    return min(later) if later else None


def _automatic_final_score(
    player_id: str,
    opportunities: tuple[GameOpportunity, ...],
) -> float:
    """Return the automatic final score for one roster player not selected by the oracle."""

    player_opportunities = tuple(
        opportunity for opportunity in opportunities if opportunity.sleeper_player_id == player_id
    )
    if not player_opportunities:
        return 0.0
    final = max(player_opportunities, key=lambda item: (item.scheduled_start, item.game_id))
    if final.rostered_at_tipoff is not True or final.completed_fantasy_score is None:
        return 0.0
    return final.completed_fantasy_score


__all__ = ("oracle_feasibility_checks", "oracle_from_planning_state")
