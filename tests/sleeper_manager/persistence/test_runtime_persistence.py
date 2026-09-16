import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    DEFAULT_SCHEDULED_WORK_LEASE,
    CachedNBARecord,
    DueWorkKind,
    ProjectionObservationRecord,
    RecommendationRecord,
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)

NOW = datetime(2026, 8, 25, 13, tzinfo=UTC)


def _work(*, due_at: datetime = NOW, dedupe_key: str = "daily:2026-08-25") -> ScheduledWorkRecord:
    return ScheduledWorkRecord(
        work_id="work-1",
        dedupe_key=dedupe_key,
        kind=DueWorkKind.DAILY,
        due_at=due_at,
        status=ScheduledWorkStatus.PENDING,
        local_day="2026-08-25",
        created_at=NOW,
        updated_at=NOW,
    )


def _recommendation(identifier: str, idempotency_key: str) -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id=identifier,
        idempotency_key=idempotency_key,
        league_id="league-1",
        fantasy_week=1,
        player_id="weekly-lineup",
        game_id=None,
        decision_type="weekly_lineup_plan",
        title="Plan",
        message="Move one player",
        deadline=NOW + timedelta(hours=2),
        policy_version="policy-v1",
        created_at=NOW,
    )


def test_sqlite_runtime_state_round_trips_and_reclaims_expired_work(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        policy = default_runtime_policy(history_version="history-2026")
        policy_record = RuntimePolicyRecord(policy.version, policy.to_json(), NOW)
        await repository.save_runtime_policy(policy_record)
        assert await repository.load_runtime_policy() == policy_record

        cache_record = CachedNBARecord(
            cache_key="schedule:12",
            provider="espn",
            resource="team-schedule:12",
            schema_version="1",
            payload_json="[]",
            retrieved_at=NOW,
            source_updated_at=None,
            expires_at=NOW + timedelta(hours=12),
            quality=DataQualityState.FRESH,
        )
        await repository.put(cache_record)
        assert await repository.get(cache_record.cache_key, now=NOW) == cache_record
        stale = await repository.get(cache_record.cache_key, now=NOW + timedelta(hours=13))
        assert stale is not None and stale.quality is DataQualityState.STALE

        observation = ProjectionObservationRecord(
            history_version="history-2026",
            player_id="401",
            game_id="game-1",
            game_start=NOW - timedelta(days=1),
            outcome_finalized_at=NOW - timedelta(hours=20),
            minutes=30,
            started=True,
            did_not_play=False,
            box_score_json=json.dumps({"points": 20}),
            source_version="espn-v1",
        )
        await repository.save_projection_observations((observation,))
        paged = await repository.load_projection_observations(
            "history-2026",
            page_size=1,
        )
        assert paged == (observation,)
        assert await repository.load_projection_observations("history-2026") == (observation,)

        await repository.upsert_scheduled_work(_work())
        first = await repository.claim_due_work(NOW, correlation_id="run-1")
        assert len(first) == 1
        assert first[0].attempt_count == 1
        assert first[0].lease_expires_at == NOW + DEFAULT_SCHEDULED_WORK_LEASE
        assert await repository.claim_due_work(NOW, correlation_id="overlap") == ()
        assert (
            await repository.claim_due_work(NOW + timedelta(minutes=5), correlation_id="cron") == ()
        )
        reclaimed = await repository.claim_due_work(
            NOW + DEFAULT_SCHEDULED_WORK_LEASE, correlation_id="run-2"
        )
        assert len(reclaimed) == 1 and reclaimed[0].attempt_count == 2
        assert await repository.finish_scheduled_work(
            reclaimed[0].work_id,
            status=ScheduledWorkStatus.COMPLETED,
            finished_at=NOW + DEFAULT_SCHEDULED_WORK_LEASE,
            correlation_id="run-2",
            terminal_summary_json='{"status":"success"}',
        )
        assert (await repository.list_scheduled_work())[0].status is ScheduledWorkStatus.COMPLETED
        completed = (await repository.list_scheduled_work())[0]
        await repository.upsert_scheduled_work(_work())
        assert (await repository.list_scheduled_work())[0].updated_at == completed.updated_at

        expired = replace(
            _work(dedupe_key="delivery-retry:expired"),
            work_id="work-expired",
            kind=DueWorkKind.DELIVERY_RETRY,
            deadline=NOW,
        )
        await repository.upsert_scheduled_work(expired)
        assert await repository.cancel_expired_scheduled_work(NOW) == 1
        stored_work = await repository.list_scheduled_work()
        assert stored_work[1].status is ScheduledWorkStatus.CANCELED
        assert stored_work[1].failure_category == "deadline_elapsed"

        first_recommendation = _recommendation("rec-1", "material-1")
        second_recommendation = _recommendation("rec-2", "material-2")
        assert await repository.create_recommendation(first_recommendation)
        assert not await repository.create_recommendation(first_recommendation)
        assert await repository.create_recommendation(second_recommendation)
        stored_first = await repository.get_recommendation("rec-1")
        stored_second = await repository.get_recommendation("rec-2")
        assert stored_first is not None and stored_first.revision == 1
        assert stored_second is not None and stored_second.revision == 2

    asyncio.run(exercise())


def test_delivery_claim_is_reclaimable_after_two_minutes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        assert await repository.create_recommendation(_recommendation("rec-1", "material-1"))
        assert await repository.claim_delivery("rec-1", NOW)
        assert not await repository.claim_delivery("rec-1", NOW + timedelta(seconds=119))
        assert await repository.claim_delivery("rec-1", NOW + timedelta(minutes=2))

    asyncio.run(exercise())
