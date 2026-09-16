"""SQLite and D1 repository coverage for load_acknowledged_decisions."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import ACKNOWLEDGEMENT_PROVENANCE
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import AcknowledgementAction, AcknowledgementOutcome
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from sleeper_manager.persistence.tokens import hash_action_token
from tests.sleeper_manager.persistence.acknowledgement_query_support import (
    AS_OF,
    _acknowledge,
    _Backend,
    _lock_trace,
    _pass_trace,
    _recommendation,
)


@pytest.fixture(params=["sqlite", "d1"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    return _Backend(request.param, tmp_path)


def test_empty_league_week_returns_no_acknowledgements(backend: _Backend) -> None:
    assert backend.load() == ()


def test_league_and_week_are_isolated(backend: _Backend) -> None:
    _acknowledge(
        backend,
        _recommendation(),
        AcknowledgementAction.LOCKED,
        token="lock-token",
        at=AS_OF - timedelta(minutes=1),
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-other-league",
            idempotency_key="idemp-other-league",
            league_id="league-2",
        ),
        AcknowledgementAction.LOCKED,
        token="other-league",
        at=AS_OF - timedelta(minutes=1),
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-other-week",
            idempotency_key="idemp-other-week",
            fantasy_week=2,
            game_id="g2",
        ),
        AcknowledgementAction.LOCKED,
        token="other-week",
        at=AS_OF - timedelta(minutes=1),
    )

    loaded = backend.load("league-1", 1)
    assert [item.decision_id for item in loaded] == ["rec-1"]


def test_as_of_includes_boundary_and_excludes_future(backend: _Backend) -> None:
    _acknowledge(
        backend,
        _recommendation(),
        AcknowledgementAction.LOCKED,
        token="boundary",
        at=AS_OF,
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-future",
            idempotency_key="idemp-future",
            game_id="g2",
        ),
        AcknowledgementAction.LOCKED,
        token="future",
        at=AS_OF + timedelta(seconds=1),
    )

    loaded = backend.load(as_of=AS_OF)
    assert [item.decision_id for item in loaded] == ["rec-1"]
    assert loaded[0].decided_at == AS_OF


def test_canonical_lock_and_pass_round_trip(backend: _Backend) -> None:
    decided_lock = AS_OF - timedelta(hours=2)
    decided_pass = AS_OF - timedelta(hours=1)
    _acknowledge(
        backend,
        _recommendation(trace_json=_lock_trace()),
        AcknowledgementAction.LOCKED,
        token="lock",
        at=decided_lock,
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-pass",
            idempotency_key="idemp-pass",
            player_id="p2",
            game_id="g2",
            trace_json=_pass_trace(),
        ),
        AcknowledgementAction.PASSED,
        token="pass",
        at=decided_pass,
    )

    loaded = backend.load()
    assert loaded == (
        AcknowledgedDecisionEvidence(
            decision_id="rec-1",
            player_id="p1",
            game_id="g1",
            action=AcknowledgedAction.LOCK,
            decided_at=decided_lock,
            provenance=ACKNOWLEDGEMENT_PROVENANCE,
            slot_index=2,
            slot_position="UTIL",
            accepted_fantasy_score=34.7,
        ),
        AcknowledgedDecisionEvidence(
            decision_id="rec-pass",
            player_id="p2",
            game_id="g2",
            action=AcknowledgedAction.PASS,
            decided_at=decided_pass,
            provenance=ACKNOWLEDGEMENT_PROVENANCE,
        ),
    )


def test_placeholder_and_weekly_lineup_records_are_excluded(backend: _Backend) -> None:
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-placeholder",
            idempotency_key="idemp-placeholder",
            decision_type="placeholder_lock_in",
        ),
        AcknowledgementAction.LOCKED,
        token="placeholder",
        at=AS_OF - timedelta(minutes=1),
    )
    _acknowledge(
        backend,
        _recommendation(
            recommendation_id="rec-lineup",
            idempotency_key="idemp-lineup",
            player_id="p2",
            decision_type="weekly_lineup",
            trace_json=_pass_trace(),
        ),
        AcknowledgementAction.PASSED,
        token="lineup",
        at=AS_OF - timedelta(minutes=1),
    )

    assert backend.load() == ()


def test_duplicate_token_replay_returns_one_constraint(backend: _Backend) -> None:
    record = _recommendation()
    first = _acknowledge(
        backend,
        record,
        AcknowledgementAction.LOCKED,
        token="once",
        at=AS_OF - timedelta(minutes=1),
    )
    replay = backend.call(
        "consume_action_token",
        hash_action_token("once"),
        AcknowledgementAction.LOCKED,
        AS_OF,
    )

    assert first is AcknowledgementOutcome.APPLIED
    assert replay.outcome is AcknowledgementOutcome.ALREADY_USED  # type: ignore[union-attr]
    assert [item.decision_id for item in backend.load()] == ["rec-1"]


def test_malformed_trace_loads_as_unreconciled_evidence(backend: _Backend) -> None:
    _acknowledge(
        backend,
        _recommendation(trace_json="{"),
        AcknowledgementAction.LOCKED,
        token="bad-trace",
        at=AS_OF - timedelta(minutes=1),
    )

    loaded = backend.load()
    assert len(loaded) == 1
    assert loaded[0].reconciled is False
    assert loaded[0].decision_id == "rec-1"


def test_direct_sql_duplicate_column_mismatch_is_unreconciled(backend: _Backend) -> None:
    # Public writes refuse this inconsistency; mutate the duplicated recommendation copy.
    _acknowledge(
        backend,
        _recommendation(),
        AcknowledgementAction.LOCKED,
        token="mismatch",
        at=AS_OF - timedelta(minutes=1),
    )
    backend.execute(
        """
        UPDATE recommendations
        SET status = ?, acknowledged_action = ?
        WHERE recommendation_id = ?
        """,
        ("pending", "passed", "rec-1"),
    )

    loaded = backend.load()
    assert loaded[0].reconciled is False
    assert loaded[0].action is AcknowledgedAction.LOCK


def test_sqlite_index_leading_columns(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    with repository._connect() as connection:
        columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(recommendations_league_week_decision_status_idx)"
            )
        ]
    assert columns[:3] == ["league_id", "fantasy_week", "decision_type"]
    assert columns[-1] == "status"


def test_async_sqlite_matches_sync_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    sync = SQLiteStateRepository(path)
    sync.initialize()
    backend = _Backend("sqlite", tmp_path)
    backend.repo = sync
    backend.path = path
    _acknowledge(
        backend,
        _recommendation(),
        AcknowledgementAction.LOCKED,
        token="durable",
        at=AS_OF - timedelta(minutes=1),
    )
    expected = sync.load_acknowledged_decisions("league-1", 1, as_of=AS_OF)

    reopened = AsyncSQLiteStateRepository(path)
    actual = asyncio.run(reopened.load_acknowledged_decisions("league-1", 1, as_of=AS_OF))
    assert actual == expected
