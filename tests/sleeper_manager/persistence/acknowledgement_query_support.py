"""Shared acknowledgement query fixtures for decoder and repository tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from sleeper_manager.domain.planning import AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import (
    LOCK_IN_DECISION_TYPE,
    AcknowledgementRawRow,
)
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    AcknowledgementResult,
    ActionTokenRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from sleeper_manager.persistence.tokens import hash_action_token
from tests.sleeper_manager.persistence.test_d1 import FakeD1

AS_OF = datetime(2026, 1, 7, 18, tzinfo=UTC)
EASTERN = timezone(timedelta(hours=-5))


def _row(
    *,
    recommendation_id: object = "rec-1",
    player_id: object = "p1",
    game_id: object = "g1",
    recommendation_status: object = "acknowledged",
    recommendation_action: object = "locked",
    recommendation_acknowledged_at: object = AS_OF.isoformat(),
    trace_json: object | None = None,
    acknowledgement_action: object = "locked",
    acknowledged_at: object = AS_OF.isoformat(),
) -> AcknowledgementRawRow:
    if trace_json is None:
        trace_json = _lock_trace()
    return AcknowledgementRawRow(
        recommendation_id=recommendation_id,
        player_id=player_id,
        game_id=game_id,
        recommendation_status=recommendation_status,
        recommendation_action=recommendation_action,
        recommendation_acknowledged_at=recommendation_acknowledged_at,
        trace_json=trace_json,
        acknowledgement_action=acknowledgement_action,
        acknowledged_at=acknowledged_at,
    )


def _lock_trace(
    *,
    slot_index: object = 2,
    slot_position: object = "UTIL",
    accepted_fantasy_score: object = 34.7,
    schema_version: object = 1,
    omit: tuple[str, ...] = (),
    extra_top_level: dict[str, object] | None = None,
    extra_fragment: dict[str, object] | None = None,
) -> str:
    fragment: dict[str, object] = {
        "schema_version": schema_version,
        "slot_index": slot_index,
        "slot_position": slot_position,
        "accepted_fantasy_score": accepted_fantasy_score,
    }
    for key in omit:
        fragment.pop(key, None)
    if extra_fragment:
        fragment.update(extra_fragment)
    payload: dict[str, object] = {"acknowledgement": fragment}
    if extra_top_level:
        payload.update(extra_top_level)
    return json.dumps(payload)


def _pass_trace(
    *,
    schema_version: object = 1,
    extra_fragment: dict[str, object] | None = None,
    extra_top_level: dict[str, object] | None = None,
) -> str:
    fragment: dict[str, object] = {"schema_version": schema_version}
    if extra_fragment:
        fragment.update(extra_fragment)
    payload: dict[str, object] = {"acknowledgement": fragment}
    if extra_top_level:
        payload.update(extra_top_level)
    return json.dumps(payload)


class _Backend:
    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        if kind == "sqlite":
            self.path = tmp_path / "state.db"
            self.repo: SQLiteStateRepository | D1StateRepository = SQLiteStateRepository(self.path)
            self.repo.initialize()
            self.database: FakeD1 | None = None
        else:
            self.path = tmp_path / "d1.db"
            self.database = FakeD1()
            asyncio.run(self.database.exec(D1_SCHEMA))
            self.repo = D1StateRepository(self.database)

    def call(self, method: str, *args: object, **kwargs: object) -> object:
        result = getattr(self.repo, method)(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    def load(
        self, league_id: str = "league-1", week: int = 1, *, as_of: datetime = AS_OF
    ) -> tuple[AcknowledgedDecisionEvidence, ...]:
        loaded = self.call("load_acknowledged_decisions", league_id, week, as_of=as_of)
        assert isinstance(loaded, tuple)
        return loaded

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if self.kind == "sqlite":
            assert isinstance(self.repo, SQLiteStateRepository)
            with self.repo._connect() as connection:
                cursor = connection.execute(sql, params)
                connection.commit()
                return cursor
        assert self.database is not None
        cursor = self.database.connection.execute(sql, params)
        self.database.connection.commit()
        return cursor


def _recommendation(**overrides: object) -> RecommendationRecord:
    values: dict[str, object] = {
        "recommendation_id": "rec-1",
        "idempotency_key": "idemp-1",
        "league_id": "league-1",
        "fantasy_week": 1,
        "player_id": "p1",
        "game_id": "g1",
        "decision_type": LOCK_IN_DECISION_TYPE,
        "title": "Lock",
        "message": "Lock the player",
        "deadline": AS_OF + timedelta(hours=1),
        "policy_version": "v1",
        "created_at": AS_OF - timedelta(hours=1),
        "trace_json": _lock_trace(),
    }
    values.update(overrides)
    return RecommendationRecord(**values)  # type: ignore[arg-type]


def _acknowledge(
    backend: _Backend,
    record: RecommendationRecord,
    action: AcknowledgementAction,
    *,
    token: str,
    at: datetime,
) -> AcknowledgementOutcome:
    backend.call("create_recommendation", record)
    backend.call(
        "create_action_token",
        ActionTokenRecord(
            token_hash=hash_action_token(token),
            recommendation_id=record.recommendation_id,
            action=action,
            created_at=record.created_at,
            expires_at=record.deadline or at,
        ),
    )
    result = backend.call("consume_action_token", hash_action_token(token), action, at)
    assert isinstance(result, AcknowledgementResult)
    return result.outcome
