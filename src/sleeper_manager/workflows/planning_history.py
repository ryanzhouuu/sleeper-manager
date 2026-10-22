"""Reconcile durable player-game observations without turning delivered advice into facts."""

from dataclasses import replace
from datetime import datetime

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import GameOpportunity, ObservedStarter, PlanningGameStatus
from sleeper_manager.domain.player_game_evidence import PlayerGameEvidence
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


def player_game_evidence(record: LockInOpportunityRecord) -> PlayerGameEvidence:
    """Expose stable, week-scoped persisted facts at the planning boundary."""
    return PlayerGameEvidence(
        player_id=record.key.player_id,
        game_id=record.key.game_id,
        roster_id=record.key.roster_id,
        status=record.status,
        observed_at=record.updated_at,
        rostered_at_tipoff=record.rostered_at_tipoff,
        roster_evidence_at=record.roster_evidence_at,
        action_deadline=record.action_deadline,
        stable_score=record.stable_score,
        stabilized_at=record.stabilized_at,
        score_is_current=(
            record.stable_fingerprint is not None
            and record.stable_fingerprint == record.current_observation_fingerprint
        ),
    )


def reconcile_player_games(
    opportunities: list[GameOpportunity],
    evidence: tuple[PlayerGameEvidence, ...],
    observed: tuple[ObservedStarter, ...],
    decision_time: datetime,
) -> tuple[GameOpportunity, ...]:
    """Use retained eligible finals only while their player remains an observed starter."""
    by_key = {(row.player_id, row.game_id, row.roster_id): row for row in evidence}
    starters = {row.player_id for row in observed}
    result = []
    for game in opportunities:
        row = by_key.get((game.sleeper_player_id, game.game_id, game.roster_id))
        if row is None or row.observed_at > decision_time:
            result.append(game)
            continue
        if game.scheduled_start > decision_time:
            result.append(game)
            continue
        terminal_lock = row.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        eligible = (
            row.rostered_at_tipoff is True
            and row.status
            not in {
                LockInOpportunityStatus.INELIGIBLE,
                LockInOpportunityStatus.EXPIRED,
                LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
            }
            and decision_time < row.action_deadline
            and game.sleeper_player_id in starters
        ) or terminal_lock
        score = (
            row.stable_score
            if row.score_is_current
            and row.stabilized_at is not None
            and row.stabilized_at <= decision_time
            else None
        )
        result.append(
            replace(
                game,
                rostered_at_tipoff=row.rostered_at_tipoff,
                eligible_slot_indices=game.eligible_slot_indices
                if eligible or game.scheduled_start > decision_time
                else (),
                status=_retained_status(game, row),
                completed_fantasy_score=score,
                finalized_at=row.stabilized_at if score is not None else game.finalized_at,
                data_quality="retained-roster-observations",
            )
        )
    finalized = {
        game.game_id: game.finalized_at
        for game in result
        if game.status is PlanningGameStatus.FINAL
    }
    active = {game.game_id for game in result if game.status is PlanningGameStatus.ACTIVE}
    return tuple(
        replace(
            game,
            status=PlanningGameStatus.FINAL,
            finalized_at=game.finalized_at or finalized[game.game_id],
        )
        if game.game_id in finalized
        else replace(game, status=PlanningGameStatus.ACTIVE)
        if game.game_id in active
        and game.status in {PlanningGameStatus.SCHEDULED, PlanningGameStatus.UNKNOWN}
        else game
        for game in result
    )


def _retained_status(game: GameOpportunity, row: PlayerGameEvidence) -> PlanningGameStatus:
    """Retain verified game progress even while a score is awaiting stabilization."""
    if row.stable_score is not None or row.status in {
        LockInOpportunityStatus.FINALIZING,
        LockInOpportunityStatus.ACTIONABLE,
        LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
        LockInOpportunityStatus.AUTOMATIC_FINAL,
    }:
        return PlanningGameStatus.FINAL
    if row.status is LockInOpportunityStatus.ACTIVE and game.status in {
        PlanningGameStatus.SCHEDULED,
        PlanningGameStatus.UNKNOWN,
    }:
        return PlanningGameStatus.ACTIVE
    return game.status
