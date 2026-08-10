from datetime import UTC, datetime, timedelta
from pathlib import Path

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    ActionTokenRecord,
    DataFreshnessRecord,
    DeliveryAttemptRecord,
    LeagueSnapshotRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from sleeper_manager.persistence.tokens import hash_action_token

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def recommendation() -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id="recommendation-1",
        idempotency_key="league:1:player:game:lock_in:policy",
        league_id="league-1",
        fantasy_week=1,
        player_id="player-1",
        game_id="game-1",
        decision_type="placeholder_lock_in",
        title="Lock player",
        message="Lock the placeholder player",
        deadline=NOW + timedelta(hours=1),
        policy_version="policy-1",
        created_at=NOW,
    )


def test_records_lock_acknowledgement(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()

    repository.record_lock_acknowledgement(
        "recommendation-1",
        "player-1",
        datetime.now(UTC),
    )

    assert repository.is_locked("recommendation-1")
    assert not repository.is_locked("recommendation-2")


def test_recommendation_creation_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    record = recommendation()

    assert repository.create_recommendation(record)
    assert not repository.create_recommendation(record)
    assert repository.get_recommendation(record.recommendation_id) == record


def test_action_token_is_consumed_once_and_locks_record(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    record = recommendation()
    repository.create_recommendation(record)
    raw_token = "test-token"
    repository.create_action_token(
        ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            recommendation_id=record.recommendation_id,
            action=AcknowledgementAction.LOCKED,
            created_at=NOW,
            expires_at=record.deadline or NOW,
        )
    )

    result = repository.consume_action_token(
        hash_action_token(raw_token), AcknowledgementAction.LOCKED, NOW + timedelta(minutes=1)
    )
    replay = repository.consume_action_token(
        hash_action_token(raw_token), AcknowledgementAction.LOCKED, NOW + timedelta(minutes=2)
    )

    assert result.outcome is AcknowledgementOutcome.APPLIED
    assert replay.outcome is AcknowledgementOutcome.ALREADY_USED
    assert repository.is_locked(record.recommendation_id)
    assert result.recommendation is not None
    assert result.recommendation.acknowledged_action is AcknowledgementAction.LOCKED

    with repository._connect() as connection:
        token_used_at = connection.execute(
            "SELECT used_at FROM action_tokens WHERE token_hash = ?",
            (hash_action_token(raw_token),),
        ).fetchone()[0]
        lock_acknowledged_at = connection.execute(
            "SELECT acknowledged_at FROM lock_acknowledgements WHERE recommendation_id = ?",
            (record.recommendation_id,),
        ).fetchone()[0]

    assert token_used_at == (NOW + timedelta(minutes=1)).isoformat()
    assert lock_acknowledged_at == (NOW + timedelta(minutes=1)).isoformat()


def test_action_token_rejects_wrong_action_and_expiration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    record = recommendation()
    repository.create_recommendation(record)
    repository.create_action_token(
        ActionTokenRecord(
            token_hash=hash_action_token("passed-token"),
            recommendation_id=record.recommendation_id,
            action=AcknowledgementAction.PASSED,
            created_at=NOW,
            expires_at=record.deadline or NOW,
        )
    )
    repository.create_action_token(
        ActionTokenRecord(
            token_hash=hash_action_token("expired-token"),
            recommendation_id=record.recommendation_id,
            action=AcknowledgementAction.LOCKED,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
    )

    wrong_action = repository.consume_action_token(
        hash_action_token("passed-token"), AcknowledgementAction.LOCKED, NOW
    )
    expired = repository.consume_action_token(
        hash_action_token("expired-token"), AcknowledgementAction.LOCKED, NOW + timedelta(minutes=2)
    )

    assert wrong_action.outcome is AcknowledgementOutcome.CONFLICT
    assert expired.outcome is AcknowledgementOutcome.EXPIRED


def test_expire_recommendations_marks_pending_records(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    record = recommendation()
    repository.create_recommendation(record)

    assert repository.expire_recommendations(NOW + timedelta(hours=2)) == 1
    assert repository.get_recommendation(record.recommendation_id) is not None
    assert repository.get_recommendation(record.recommendation_id).status.value == "expired"  # type: ignore[union-attr]


def test_delivery_success_is_recorded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(Path(tmp_path) / "state.db")
    repository.initialize()
    record = recommendation()
    repository.create_recommendation(record)
    repository.record_delivery_attempt(
        DeliveryAttemptRecord(
            delivery_id="delivery-1",
            recommendation_id=record.recommendation_id,
            provider="test",
            attempt_number=1,
            attempted_at=NOW,
            succeeded=True,
        )
    )

    assert repository.has_successful_delivery(record.recommendation_id)


def test_snapshots_and_freshness_are_persisted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    snapshot = LeagueSnapshotRecord(
        snapshot_id="snapshot-1",
        league_id="league-1",
        fantasy_week=1,
        payload_json='{"roster": []}',
        retrieved_at=NOW,
    )
    freshness = DataFreshnessRecord(
        resource="scoreboard",
        retrieved_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        quality=DataQualityState.FRESH,
        warnings=("warning",),
        errors=(),
    )

    repository.save_league_snapshot(snapshot)
    repository.save_data_freshness(freshness)

    assert repository.load_league_snapshot("league-1", 1) == snapshot
    assert repository.load_data_freshness("scoreboard") == freshness
