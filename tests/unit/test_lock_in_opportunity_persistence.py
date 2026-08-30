"""Shared SQLite and fake-D1 contracts for live Lock-In opportunity state."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_d1 import FakeD1

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    ActionTokenRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInObservation,
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.persistence.tokens import hash_action_token

NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)


def _opportunity() -> LockInOpportunityRecord:
    """Build one scheduled opportunity with exact pre-tipoff evidence."""

    return LockInOpportunityRecord(
        key=LockInOpportunityKey("league", 1, 7, "player", "game"),
        provider_player_id="espn-player",
        scheduled_start=NOW - timedelta(hours=3),
        action_deadline=NOW + timedelta(hours=4),
        fantasy_week_end=NOW + timedelta(days=2),
        status=LockInOpportunityStatus.FINALIZING,
        next_check_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        slot_index=2,
        slot_position="UTIL",
        eligible_positions=("PG", "SG"),
        rostered_at_tipoff=True,
        roster_evidence_at=NOW - timedelta(hours=4),
        league_configuration_fingerprint="league-v1",
    )


async def _repository(backend: str, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Create one initialized backend behind the shared async protocol."""

    if backend == "sqlite":
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        return repository
    database = FakeD1()
    await database.exec(D1_SCHEMA)
    return D1StateRepository(database)


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_opportunity_guards_stabilization_corrections_and_queries(
    backend: str, tmp_path: Path
) -> None:
    """Keep both repositories identical across guarded lifecycle transitions."""

    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        original = _opportunity()
        assert await repository.upsert_lock_in_opportunity(original)
        assert not await repository.upsert_lock_in_opportunity(original)
        assert await repository.get_lock_in_opportunity(original.key) == original
        assert await repository.list_due_lock_in_opportunities(NOW) == (original,)

        first = await repository.record_lock_in_observation(
            original.key,
            LockInObservation(12.0, "summary-a", "wake-1", NOW, NOW + timedelta(minutes=5)),
            expected_version=0,
        )
        assert first is not None
        assert first.consecutive_direct_poll_count == 1
        assert first.stable_score is None
        assert (
            await repository.record_lock_in_observation(
                original.key,
                LockInObservation(
                    12.0,
                    "summary-a",
                    "wake-1",
                    NOW,
                    NOW + timedelta(minutes=5),
                ),
                expected_version=first.row_version,
            )
            is None
        )

        stable = await repository.record_lock_in_observation(
            original.key,
            LockInObservation(
                12.0,
                "summary-a",
                "wake-2",
                NOW + timedelta(minutes=5),
                NOW + timedelta(minutes=10),
            ),
            expected_version=first.row_version,
        )
        assert stable is not None
        assert stable.stable_score == 12.0
        assert stable.score_revision == 1
        assert stable.stabilized_at == NOW + timedelta(minutes=5)

        correction = await repository.record_lock_in_observation(
            original.key,
            LockInObservation(
                13.0,
                "summary-b",
                "wake-3",
                NOW + timedelta(minutes=10),
                NOW + timedelta(minutes=15),
            ),
            expected_version=stable.row_version,
        )
        assert correction is not None
        assert correction.stable_score == 12.0
        assert correction.consecutive_direct_poll_count == 1
        corrected = await repository.record_lock_in_observation(
            original.key,
            LockInObservation(
                13.0,
                "summary-b",
                "wake-4",
                NOW + timedelta(minutes=15),
                NOW + timedelta(minutes=20),
            ),
            expected_version=correction.row_version,
        )
        assert corrected is not None
        assert corrected.stable_score == 13.0
        assert corrected.score_revision == 2

        actionable = replace(
            corrected,
            status=LockInOpportunityStatus.ACTIONABLE,
            latest_evaluation_hash="evaluation-v2",
            updated_at=NOW + timedelta(minutes=16),
        )
        assert await repository.update_lock_in_opportunity(
            actionable,
            expected_version=corrected.row_version,
        )
        assert not await repository.update_lock_in_opportunity(
            actionable,
            expected_version=corrected.row_version,
        )
        listed = await repository.list_actionable_lock_in_opportunities("league", 1)
        assert len(listed) == 1 and listed[0].latest_evaluation_hash == "evaluation-v2"

        acknowledged = replace(
            listed[0],
            status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
            acknowledged_action="locked",
            acknowledged_at=NOW + timedelta(minutes=17),
            updated_at=NOW + timedelta(minutes=17),
        )
        assert await repository.update_lock_in_opportunity(
            acknowledged,
            expected_version=listed[0].row_version,
        )
        fixed = await repository.load_acknowledged_lock_in_opportunities("league", 1)
        assert len(fixed) == 1 and fixed[0].stable_score == 13.0
        assert await repository.expire_lock_in_opportunities(NOW + timedelta(hours=4)) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_consume_acknowledgement_updates_opportunity_atomically(
    backend: str, tmp_path: Path
) -> None:
    """Apply recommendation and opportunity acknowledgement in one repository call."""

    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        recommendation = RecommendationRecord(
            recommendation_id="rec-lock",
            idempotency_key="lock-key",
            league_id="league",
            fantasy_week=1,
            player_id="player",
            game_id="game",
            decision_type="live_lock_in",
            title="Lock player?",
            message="Lock now",
            deadline=NOW + timedelta(hours=2),
            policy_version="policy-1",
            created_at=NOW,
        )
        await repository.create_recommendation(recommendation)
        original = replace(
            _opportunity(),
            status=LockInOpportunityStatus.ACTIONABLE,
            current_recommendation_id=recommendation.recommendation_id,
            current_recommendation_kind="live_lock_in",
            stable_score=18.0,
        )
        assert await repository.upsert_lock_in_opportunity(original)
        raw_token = "lock-token"
        await repository.create_action_token(
            ActionTokenRecord(
                token_hash=hash_action_token(raw_token),
                recommendation_id=recommendation.recommendation_id,
                action=AcknowledgementAction.LOCKED,
                created_at=NOW,
                expires_at=recommendation.deadline or NOW,
            )
        )
        result = await repository.consume_action_token(
            hash_action_token(raw_token),
            AcknowledgementAction.LOCKED,
            NOW + timedelta(minutes=1),
        )
        stored = await repository.get_lock_in_opportunity(original.key)
        assert result.outcome is AcknowledgementOutcome.APPLIED
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        assert stored.acknowledged_action == "locked"
        assert stored.stable_score == 18.0
        assert await repository.has_open_lock_in_watch("game", NOW) is False

    asyncio.run(exercise())


def test_phase_six_migration_preserves_existing_scheduled_work() -> None:
    """Rebuild the constrained work table without losing populated rows."""

    database = FakeD1()
    migrations = Path("infra/cloudflare/migrations")
    asyncio.run(database.exec((migrations / "0001_phase3.sql").read_text()))
    asyncio.run(
        database.exec((migrations / "0002_acknowledged_team_week_decisions.sql").read_text())
    )
    asyncio.run(database.exec((migrations / "0003_scheduled_work.sql").read_text()))
    database.connection.execute(
        """
        INSERT INTO scheduled_work (
            work_id, dedupe_key, kind, due_at, status, created_at, updated_at
        ) VALUES ('existing', 'daily:existing', 'daily', ?, 'pending', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    database.connection.commit()

    asyncio.run(database.exec((migrations / "0004_live_lock_in.sql").read_text()))
    database.connection.execute(
        """
        INSERT INTO scheduled_work (
            work_id, dedupe_key, kind, due_at, status, created_at, updated_at
        ) VALUES ('postgame', 'postgame:game', 'postgame', ?, 'pending', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )

    kinds = tuple(
        row[0]
        for row in database.connection.execute(
            "SELECT kind FROM scheduled_work ORDER BY work_id"
        ).fetchall()
    )
    assert kinds == ("daily", "postgame")
