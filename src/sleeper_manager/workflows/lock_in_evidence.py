"""Convert terminal live Lock-In rows into later-planning acknowledgement evidence."""

from __future__ import annotations

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord

LOCK_IN_OPPORTUNITY_PROVENANCE = "lock_in_opportunity_v1"


def evidence_from_lock_in_opportunity(
    record: LockInOpportunityRecord,
) -> AcknowledgedDecisionEvidence | None:
    """Return fixed Lock or Pass evidence for later planning, or None if not terminal."""

    if record.status is LockInOpportunityStatus.ACKNOWLEDGED_PASSED:
        return AcknowledgedDecisionEvidence(
            decision_id=_decision_id(record),
            player_id=record.key.player_id,
            game_id=record.key.game_id,
            action=AcknowledgedAction.PASS,
            decided_at=record.acknowledged_at or record.updated_at,
            provenance=LOCK_IN_OPPORTUNITY_PROVENANCE,
        )
    if record.status in {
        LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        LockInOpportunityStatus.AUTOMATIC_FINAL,
    }:
        reconciled = (
            record.slot_index is not None
            and record.slot_position is not None
            and record.stable_score is not None
        )
        return AcknowledgedDecisionEvidence(
            decision_id=_decision_id(record),
            player_id=record.key.player_id,
            game_id=record.key.game_id,
            action=AcknowledgedAction.LOCK,
            decided_at=record.acknowledged_at or record.stabilized_at or record.updated_at,
            provenance=LOCK_IN_OPPORTUNITY_PROVENANCE,
            slot_index=record.slot_index,
            slot_position=record.slot_position,
            accepted_fantasy_score=record.stable_score,
            reconciled=reconciled,
        )
    return None


def merge_lock_in_opportunity_evidence(
    existing: tuple[AcknowledgedDecisionEvidence, ...],
    records: tuple[LockInOpportunityRecord, ...],
) -> tuple[AcknowledgedDecisionEvidence, ...]:
    """Prefer current opportunity scores over recommendation-trace snapshots."""

    converted: list[AcknowledgedDecisionEvidence] = []
    keys: set[tuple[str, str]] = set()
    for record in records:
        evidence = evidence_from_lock_in_opportunity(record)
        if evidence is None:
            continue
        converted.append(evidence)
        keys.add((evidence.player_id, evidence.game_id))
    kept = tuple(item for item in existing if (item.player_id, item.game_id) not in keys)
    return kept + tuple(converted)


def _decision_id(record: LockInOpportunityRecord) -> str:
    """Reuse the recommendation identity when present; otherwise name the automatic final."""

    if record.current_recommendation_id:
        return record.current_recommendation_id
    key = record.key
    return (
        f"automatic-final:{key.league_id}:{key.fantasy_week}:{key.roster_id}:"
        f"{key.player_id}:{key.game_id}"
    )


__all__ = [
    "LOCK_IN_OPPORTUNITY_PROVENANCE",
    "evidence_from_lock_in_opportunity",
    "merge_lock_in_opportunity_evidence",
]
