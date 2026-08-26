"""Pin D1 repository branches that the later shared-SQL extraction must preserve."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_d1 import NOW, make_repository, recommendation

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.acknowledgements import AcknowledgementQueryError
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    ActionTokenRecord,
    CachedNBARecord,
    DeliveryAttemptRecord,
    DueWorkKind,
    ProjectionObservationRecord,
    RecommendationStatus,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.tokens import hash_action_token


def _token(record, *, action: AcknowledgementAction, raw: str = "token") -> ActionTokenRecord:
    return ActionTokenRecord(
        token_hash=hash_action_token(raw),
        recommendation_id=record.recommendation_id,
        action=action,
        created_at=NOW,
        expires_at=record.deadline or NOW + timedelta(hours=1),
    )


def _work(**overrides: object) -> ScheduledWorkRecord:
    values: dict[str, object] = {
        "work_id": "work-1",
        "dedupe_key": "daily:2026-08-08",
        "kind": DueWorkKind.DAILY,
        "due_at": NOW,
        "status": ScheduledWorkStatus.PENDING,
        "local_day": "2026-08-08",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return ScheduledWorkRecord(**values)  # type: ignore[arg-type]


def _observation(**overrides: object) -> ProjectionObservationRecord:
    values: dict[str, object] = {
        "history_version": "history-v1",
        "player_id": "401",
        "game_id": "game-1",
        "game_start": NOW - timedelta(days=2),
        "outcome_finalized_at": NOW - timedelta(days=1),
        "minutes": 30.0,
        "started": True,
        "did_not_play": False,
        "box_score_json": '{"points":20}',
        "source_version": "espn-v1",
    }
    values.update(overrides)
    return ProjectionObservationRecord(**values)  # type: ignore[arg-type]


def _run_returning(payload: object) -> D1StateRepository:
    class _Statement:
        def bind(self, *params: object) -> object:
            return self

        async def run(self) -> object:
            return payload

        async def first(self) -> object:
            return payload

    class _Database:
        def prepare(self, query: str) -> object:
            return _Statement()

    return D1StateRepository(_Database())


def test_initialize_is_a_no_op() -> None:
    _, repository = make_repository()
    assert asyncio.run(repository.initialize()) is None


def test_missing_snapshot_and_freshness_and_cache_return_none() -> None:
    _, repository = make_repository()
    assert asyncio.run(repository.load_league_snapshot("league-1", 1)) is None
    assert asyncio.run(repository.load_data_freshness("scoreboard")) is None
    assert asyncio.run(repository.load_runtime_policy()) is None
    assert asyncio.run(repository.get("missing", now=NOW)) is None


def test_run_without_meta_counts_as_zero_changes() -> None:
    repository = _run_returning({"success": True})
    assert asyncio.run(repository.create_recommendation(recommendation())) is False


def test_run_with_empty_meta_counts_as_zero_changes() -> None:
    repository = _run_returning({"success": True, "meta": {}})
    assert asyncio.run(repository.create_recommendation(recommendation())) is False


def test_success_neither_true_nor_false_is_an_unexpected_envelope() -> None:
    repository = _run_returning({"success": "yes"})
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(repository.create_recommendation(recommendation()))


def test_first_row_that_is_not_a_mapping_is_an_unexpected_envelope() -> None:
    repository = _run_returning(["not-a-row"])
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(repository.get_recommendation("recommendation-1"))


def test_delivery_attempts_exclude_claim_rows_from_attempt_numbers() -> None:
    _, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))

    assert asyncio.run(repository.next_delivery_attempt_number(record.recommendation_id)) == 1
    assert asyncio.run(repository.has_successful_delivery(record.recommendation_id)) is False
    assert asyncio.run(repository.claim_delivery(record.recommendation_id, NOW)) is True
    assert asyncio.run(repository.claim_delivery(record.recommendation_id, NOW)) is False
    assert asyncio.run(repository.next_delivery_attempt_number(record.recommendation_id)) == 1

    asyncio.run(
        repository.record_delivery_attempt(
            DeliveryAttemptRecord(
                delivery_id="delivery-1",
                recommendation_id=record.recommendation_id,
                provider="ntfy",
                attempt_number=1,
                attempted_at=NOW,
                succeeded=True,
            )
        )
    )
    assert asyncio.run(repository.has_successful_delivery(record.recommendation_id)) is True
    assert asyncio.run(repository.next_delivery_attempt_number(record.recommendation_id)) == 2
    assert asyncio.run(repository.release_delivery_claim(record.recommendation_id)) is True
    assert asyncio.run(repository.release_delivery_claim(record.recommendation_id)) is False


def test_consume_unknown_token_is_invalid() -> None:
    _, repository = make_repository()
    result = asyncio.run(
        repository.consume_action_token(
            hash_action_token("missing"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    assert result.outcome is AcknowledgementOutcome.INVALID
    assert result.recommendation is None


def test_consume_token_outcomes_when_apply_does_not_take() -> None:
    _, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))
    asyncio.run(
        repository.create_action_token(
            _token(record, action=AcknowledgementAction.PASSED, raw="pass")
        )
    )
    asyncio.run(
        repository.create_action_token(
            replace(
                _token(record, action=AcknowledgementAction.LOCKED, raw="expired"),
                expires_at=NOW,
            )
        )
    )

    conflict = asyncio.run(
        repository.consume_action_token(
            hash_action_token("pass"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    expired = asyncio.run(
        repository.consume_action_token(
            hash_action_token("expired"),
            AcknowledgementAction.LOCKED,
            NOW + timedelta(minutes=1),
        )
    )
    assert conflict.outcome is AcknowledgementOutcome.CONFLICT
    assert expired.outcome is AcknowledgementOutcome.EXPIRED

    asyncio.run(
        repository.create_action_token(
            _token(record, action=AcknowledgementAction.LOCKED, raw="stale")
        )
    )
    asyncio.run(repository.expire_recommendations(NOW + timedelta(hours=2)))
    stale = asyncio.run(
        repository.consume_action_token(
            hash_action_token("stale"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    assert stale.outcome is AcknowledgementOutcome.CONFLICT


def test_consume_token_is_invalid_when_recommendation_row_is_gone() -> None:
    database, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))
    asyncio.run(repository.create_action_token(_token(record, action=AcknowledgementAction.LOCKED)))
    database.connection.execute("PRAGMA foreign_keys = OFF")
    database.connection.execute(
        "DELETE FROM recommendations WHERE recommendation_id = ?",
        (record.recommendation_id,),
    )
    database.connection.commit()

    result = asyncio.run(
        repository.consume_action_token(
            hash_action_token("token"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    assert result.outcome is AcknowledgementOutcome.INVALID


def test_acknowledgement_outcome_fallback_for_missing_token_and_pending_conflict() -> None:
    _, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))
    asyncio.run(repository.create_action_token(_token(record, action=AcknowledgementAction.LOCKED)))

    missing = asyncio.run(
        repository._acknowledgement_outcome(
            hash_action_token("gone"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    pending = asyncio.run(
        repository._acknowledgement_outcome(
            hash_action_token("token"),
            AcknowledgementAction.LOCKED,
            NOW,
        )
    )
    assert missing.outcome is AcknowledgementOutcome.INVALID
    assert pending.outcome is AcknowledgementOutcome.CONFLICT


def test_expire_recommendations_and_direct_lock_acknowledgement() -> None:
    _, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))
    assert asyncio.run(repository.expire_recommendations(NOW + timedelta(hours=2))) == 1
    expired = asyncio.run(repository.get_recommendation(record.recommendation_id))
    assert expired is not None and expired.status is RecommendationStatus.EXPIRED

    asyncio.run(repository.record_lock_acknowledgement("rec-direct", "player-1", NOW))
    assert asyncio.run(repository.is_locked("rec-direct")) is True
    assert asyncio.run(repository.is_locked("missing")) is False


def test_list_pending_rejects_non_mapping_rows() -> None:
    _, repository = make_repository()

    async def rows(*args: object, **kwargs: object) -> tuple[object, ...]:
        return ("not-a-mapping",)

    repository._all = rows  # type: ignore[method-assign]
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(
            repository.list_pending_recommendations(
                "league-1",
                1,
                decision_type="placeholder_lock_in",
            )
        )


def test_acknowledged_decisions_accept_sequence_rows_and_reject_scalars() -> None:
    _, repository = make_repository()
    lock_trace = json.dumps(
        {
            "schema_version": 1,
            "slot_index": 2,
            "slot_position": "UTIL",
            "accepted_fantasy_score": 34.7,
        }
    )
    sequence_row = (
        "rec-1",
        "p1",
        "g1",
        "acknowledged",
        "locked",
        NOW.isoformat(),
        lock_trace,
        "locked",
        NOW.isoformat(),
    )

    async def sequence_rows(*args: object, **kwargs: object) -> tuple[object, ...]:
        return (sequence_row,)

    repository._all = sequence_rows  # type: ignore[method-assign]
    loaded = asyncio.run(repository.load_acknowledged_decisions("league-1", 1, as_of=NOW))
    assert [item.decision_id for item in loaded] == ["rec-1"]

    async def scalar_rows(*args: object, **kwargs: object) -> tuple[object, ...]:
        return ("bad-row",)

    repository._all = scalar_rows  # type: ignore[method-assign]
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(repository.load_acknowledged_decisions("league-1", 1, as_of=NOW))


def test_projection_history_empty_save_before_filter_and_paging() -> None:
    _, repository = make_repository()
    asyncio.run(repository.save_projection_observations(()))
    assert asyncio.run(repository.load_projection_observations("history-v1")) == ()

    older = _observation()
    newer = _observation(
        game_id="game-2",
        game_start=NOW - timedelta(hours=2),
        outcome_finalized_at=NOW,
    )
    asyncio.run(repository.save_projection_observations((older, newer)))

    with pytest.raises(ValueError, match="page size"):
        asyncio.run(repository.load_projection_observations("history-v1", page_size=0))

    before = asyncio.run(
        repository.load_projection_observations(
            "history-v1",
            before=NOW - timedelta(hours=12),
        )
    )
    assert [item.game_id for item in before] == ["game-1"]

    paged = asyncio.run(repository.load_projection_observations("history-v1", page_size=1))
    assert [item.game_id for item in paged] == ["game-1", "game-2"]


def test_projection_observation_row_must_be_a_mapping() -> None:
    _, repository = make_repository()

    async def rows(*args: object, **kwargs: object) -> tuple[object, ...]:
        return ("not-a-mapping",)

    repository._all = rows  # type: ignore[method-assign]
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(repository.load_projection_observations("history-v1"))


def test_scheduled_work_guards_filters_and_zero_claim_limit() -> None:
    _, repository = make_repository()
    asyncio.run(repository.upsert_scheduled_work(_work()))
    asyncio.run(
        repository.upsert_scheduled_work(
            _work(work_id="work-2", dedupe_key="pre_tipoff:g1", kind=DueWorkKind.PRE_TIPOFF)
        )
    )
    assert asyncio.run(repository.claim_due_work(NOW, correlation_id="run", limit=0)) == ()

    claimed = asyncio.run(repository.claim_due_work(NOW, correlation_id="run-1", limit=1))
    assert len(claimed) == 1
    with pytest.raises(ValueError, match="completed, retry, or canceled"):
        asyncio.run(
            repository.finish_scheduled_work(
                claimed[0].work_id,
                status=ScheduledWorkStatus.RUNNING,
                finished_at=NOW,
                correlation_id="run-1",
            )
        )
    with pytest.raises(ValueError, match="retry timestamp"):
        asyncio.run(
            repository.finish_scheduled_work(
                claimed[0].work_id,
                status=ScheduledWorkStatus.RETRY,
                finished_at=NOW,
                correlation_id="run-1",
            )
        )
    assert asyncio.run(
        repository.finish_scheduled_work(
            claimed[0].work_id,
            status=ScheduledWorkStatus.COMPLETED,
            finished_at=NOW,
            correlation_id="run-1",
        )
    )
    daily_done = asyncio.run(
        repository.list_scheduled_work(
            kind=DueWorkKind.DAILY,
            statuses=(ScheduledWorkStatus.COMPLETED,),
        )
    )
    pending_tipoff = asyncio.run(
        repository.list_scheduled_work(
            kind=DueWorkKind.PRE_TIPOFF,
            statuses=(ScheduledWorkStatus.PENDING,),
        )
    )
    assert [item.work_id for item in daily_done] == ["work-1"]
    assert [item.work_id for item in pending_tipoff] == ["work-2"]


def test_nba_cache_round_trip_still_returns_fresh_records() -> None:
    _, repository = make_repository()
    record = CachedNBARecord(
        cache_key="schedule:12",
        provider="espn",
        resource="team-schedule:12",
        schema_version="1",
        payload_json="[]",
        retrieved_at=NOW,
        source_updated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        quality=DataQualityState.FRESH,
    )
    asyncio.run(repository.put(record))
    assert asyncio.run(repository.get(record.cache_key, now=NOW)) == record
