"""D1 envelope, Worker proxy, and migration coverage for acknowledgement loads."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.persistence.acknowledgements import (
    LOCK_IN_DECISION_TYPE,
    AcknowledgementQueryError,
)
from sleeper_manager.persistence.base import AcknowledgementAction
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from tests.paths import REPO_ROOT
from tests.sleeper_manager.persistence.acknowledgement_query_support import (
    AS_OF,
    _acknowledge,
    _Backend,
    _lock_trace,
    _pass_trace,
    _recommendation,
)
from tests.sleeper_manager.persistence.test_d1 import FakeD1


def test_d1_all_returns_every_joined_row() -> None:
    database = FakeD1()
    asyncio.run(database.exec(D1_SCHEMA))
    repository = D1StateRepository(database)
    backend = _Backend("d1", Path("/tmp"))
    backend.database = database
    backend.repo = repository
    _acknowledge(
        backend,
        _recommendation(),
        AcknowledgementAction.LOCKED,
        token="one",
        at=AS_OF - timedelta(hours=1),
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-2",
            idempotency_key="idemp-2",
            player_id="p2",
            game_id="g2",
            trace_json=_pass_trace(),
        ),
        AcknowledgementAction.PASSED,
        token="two",
        at=AS_OF - timedelta(minutes=1),
    )

    loaded = asyncio.run(repository.load_acknowledged_decisions("league-1", 1, as_of=AS_OF))
    assert [item.decision_id for item in loaded] == ["rec-1", "rec-2"]


def test_d1_binds_league_week_and_decision_type() -> None:
    database = FakeD1()
    asyncio.run(database.exec(D1_SCHEMA))
    repository = D1StateRepository(database)
    asyncio.run(repository.load_acknowledged_decisions("league-9", 4, as_of=AS_OF))
    assert database.last_bound == ("league-9", 4, LOCK_IN_DECISION_TYPE)


def test_d1_unexpected_result_envelope_raises() -> None:
    class _Broken:
        def prepare(self, query: str) -> object:
            class _Statement:
                def bind(self, *params: object) -> _Statement:
                    return self

                async def all(self) -> object:
                    return {"results": "not-rows"}

            return _Statement()

    repository = D1StateRepository(_Broken())
    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio.run(repository.load_acknowledged_decisions("league-1", 1, as_of=AS_OF))


class _JsProxy:
    """Minimal Python Worker D1 proxy: not a Mapping, convertible via to_py()."""

    def __init__(self, value: object) -> None:
        self._value = value

    def to_py(self) -> object:
        return self._value


class _AttributeD1Result:
    def __init__(self, results: object, success: object = True) -> None:
        self.results = results
        self.success = success


def _joined_lock_row() -> dict[str, object]:
    return {
        "recommendation_id": "rec-1",
        "player_id": "p1",
        "game_id": "g1",
        "recommendation_status": "acknowledged",
        "recommendation_action": "locked",
        "recommendation_acknowledged_at": AS_OF.isoformat(),
        "trace_json": _lock_trace(),
        "acknowledgement_action": "locked",
        "acknowledged_at": AS_OF.isoformat(),
    }


def _repository_returning(result: object) -> D1StateRepository:
    class _Statement:
        def bind(self, *params: object) -> _Statement:
            return self

        async def all(self) -> object:
            return result

    class _Database:
        def prepare(self, query: str) -> object:
            return _Statement()

    return D1StateRepository(_Database())


def test_d1_python_worker_proxy_results_decode() -> None:
    row = _joined_lock_row()
    proxy_result = _JsProxy({"success": True, "results": [_JsProxy(row)]})
    loaded = asyncio.run(
        _repository_returning(proxy_result).load_acknowledged_decisions("league-1", 1, as_of=AS_OF)
    )
    assert [item.decision_id for item in loaded] == ["rec-1"]
    assert loaded[0].reconciled is True

    attribute_result = _AttributeD1Result(results=[_JsProxy(row)], success=True)
    loaded = asyncio.run(
        _repository_returning(attribute_result).load_acknowledged_decisions(
            "league-1", 1, as_of=AS_OF
        )
    )
    assert [item.decision_id for item in loaded] == ["rec-1"]


def test_d1_unsuccessful_query_raises_instead_of_empty_constraints() -> None:
    repository = _repository_returning({"success": False, "results": []})
    with pytest.raises(AcknowledgementQueryError, match="unsuccessful"):
        asyncio.run(repository.load_acknowledged_decisions("league-1", 1, as_of=AS_OF))


def test_migration_adds_index_without_rewriting_phase3_rows(tmp_path: Path) -> None:
    path = tmp_path / "migrated.db"
    connection = sqlite3.connect(path)
    root = REPO_ROOT
    connection.executescript(
        (root / "infra/cloudflare/migrations/0001_phase3.sql").read_text(encoding="utf-8")
    )
    connection.execute(
        """
        INSERT INTO recommendations (
            recommendation_id, idempotency_key, league_id, fantasy_week, player_id, game_id,
            decision_type, title, message, deadline, policy_version, created_at, status,
            acknowledged_action, acknowledged_at, trace_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "rec-phase3",
            "idemp-phase3",
            "league-1",
            1,
            "p1",
            "g1",
            "placeholder_lock_in",
            "Lock",
            "placeholder",
            AS_OF.isoformat(),
            "v1",
            AS_OF.isoformat(),
            "acknowledged",
            "locked",
            AS_OF.isoformat(),
            _lock_trace(),
        ),
    )
    connection.execute(
        """
        INSERT INTO action_tokens (
            token_hash, recommendation_id, action, created_at, expires_at, used_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "token-hash",
            "rec-phase3",
            "locked",
            AS_OF.isoformat(),
            AS_OF.isoformat(),
            AS_OF.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO acknowledgements (
            acknowledgement_id, recommendation_id, action, acknowledged_at, token_hash
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("ack-phase3", "rec-phase3", "locked", AS_OF.isoformat(), "token-hash"),
    )
    connection.commit()
    connection.executescript(
        (root / "infra/cloudflare/migrations/0002_acknowledged_team_week_decisions.sql").read_text(
            encoding="utf-8"
        )
    )
    remaining = connection.execute(
        "SELECT decision_type, trace_json FROM recommendations WHERE recommendation_id = ?",
        ("rec-phase3",),
    ).fetchone()
    columns = [
        row[2]
        for row in connection.execute(
            "PRAGMA index_info(recommendations_league_week_decision_status_idx)"
        )
    ]
    connection.close()

    assert remaining is not None
    assert remaining[0] == "placeholder_lock_in"
    assert columns[:3] == ["league_id", "fantasy_week", "decision_type"]

    repository = SQLiteStateRepository(path)
    assert repository.load_acknowledged_decisions("league-1", 1, as_of=AS_OF) == ()
