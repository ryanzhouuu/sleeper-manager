from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_acknowledgement_queries import _Backend

from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)


def _weekly_recommendation(**overrides: object) -> RecommendationRecord:
    values: dict[str, object] = {
        "recommendation_id": "rec-lineup-1",
        "idempotency_key": "league-1:1:weekly_lineup:hash-a",
        "league_id": "league-1",
        "fantasy_week": 1,
        "player_id": "p1",
        "game_id": "g1",
        "decision_type": WEEKLY_LINEUP_DECISION_TYPE,
        "title": "Lineup move required",
        "message": "Start Alice in UTIL.",
        "deadline": NOW + timedelta(hours=1),
        "policy_version": "policy-v1",
        "created_at": NOW,
        "trace_json": "{}",
    }
    values.update(overrides)
    return RecommendationRecord(**values)  # type: ignore[arg-type]


@pytest.fixture(params=["sqlite", "d1"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    return _Backend(request.param, tmp_path)


def test_list_pending_is_isolated_by_league_week_and_decision_type(backend: _Backend) -> None:
    backend.call("create_recommendation", _weekly_recommendation())
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-lineup-2",
            idempotency_key="league-1:1:weekly_lineup:hash-b",
            league_id="league-2",
        ),
    )
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-lineup-3",
            idempotency_key="league-1:2:weekly_lineup:hash-c",
            fantasy_week=2,
        ),
    )
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-lock-1",
            idempotency_key="idemp-lock",
            decision_type="lock_in",
        ),
    )
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-expired",
            idempotency_key="league-1:1:weekly_lineup:hash-expired",
            status=RecommendationStatus.EXPIRED,
        ),
    )

    pending = backend.call(
        "list_pending_recommendations",
        "league-1",
        1,
        decision_type=WEEKLY_LINEUP_DECISION_TYPE,
    )
    assert isinstance(pending, tuple)
    assert [record.recommendation_id for record in pending] == ["rec-lineup-1"]


def test_supersede_only_affects_pending_rows(backend: _Backend) -> None:
    backend.call("create_recommendation", _weekly_recommendation())
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-expired",
            idempotency_key="league-1:1:weekly_lineup:hash-expired",
            status=RecommendationStatus.EXPIRED,
        ),
    )
    backend.call(
        "create_recommendation",
        _weekly_recommendation(
            recommendation_id="rec-ack",
            idempotency_key="league-1:1:weekly_lineup:hash-ack",
            status=RecommendationStatus.ACKNOWLEDGED,
            acknowledged_action=AcknowledgementAction.LOCKED,
            acknowledged_at=NOW,
        ),
    )

    assert backend.call("supersede_recommendation", "rec-lineup-1", NOW) is True
    assert backend.call("supersede_recommendation", "rec-lineup-1", NOW) is False
    assert backend.call("supersede_recommendation", "rec-expired", NOW) is False
    assert backend.call("supersede_recommendation", "rec-ack", NOW) is False

    updated = backend.call("get_recommendation", "rec-lineup-1")
    assert isinstance(updated, RecommendationRecord)
    assert updated.status is RecommendationStatus.SUPERSEDED
    expired = backend.call("get_recommendation", "rec-expired")
    assert isinstance(expired, RecommendationRecord)
    assert expired.status is RecommendationStatus.EXPIRED


def test_async_sqlite_matches_sync_for_list_and_supersede(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    sync = SQLiteStateRepository(path)
    sync.initialize()
    sync.create_recommendation(_weekly_recommendation())

    reopened = AsyncSQLiteStateRepository(path)
    pending = asyncio.run(
        reopened.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
    )
    assert pending == sync.list_pending_recommendations(
        "league-1",
        1,
        decision_type=WEEKLY_LINEUP_DECISION_TYPE,
    )

    assert asyncio.run(reopened.supersede_recommendation("rec-lineup-1", NOW)) is True
    assert sync.get_recommendation("rec-lineup-1") is not None
    assert sync.get_recommendation("rec-lineup-1").status is RecommendationStatus.SUPERSEDED
