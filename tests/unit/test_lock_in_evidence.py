"""Convert terminal live Lock-In rows into later-planning acknowledgement evidence."""

from datetime import UTC, datetime

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.lock_in_evidence import (
    evidence_from_lock_in_opportunity,
    merge_lock_in_opportunity_evidence,
)


def test_lock_in_evidence_prefers_opportunity_score() -> None:
    """Corrected opportunity scores replace recommendation-trace snapshots."""

    now = datetime(2026, 1, 8, tzinfo=UTC)
    existing = AcknowledgedDecisionEvidence(
        decision_id="old",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=now,
        provenance="repository_acknowledgement_v1",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=20.0,
    )
    record = LockInOpportunityRecord(
        key=LockInOpportunityKey("league-1", 1, 1, "p1", "g1"),
        provider_player_id="401",
        scheduled_start=now,
        action_deadline=now,
        fantasy_week_end=now,
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        next_check_at=now,
        created_at=now,
        updated_at=now,
        slot_index=0,
        slot_position="PG",
        stable_score=24.0,
        acknowledged_action="locked",
        acknowledged_at=now,
        current_recommendation_id="rec-1",
    )
    merged = merge_lock_in_opportunity_evidence((existing,), (record,))
    converted = evidence_from_lock_in_opportunity(record)
    assert converted is not None
    assert merged == (converted,)
    assert merged[0].accepted_fantasy_score == 24.0
