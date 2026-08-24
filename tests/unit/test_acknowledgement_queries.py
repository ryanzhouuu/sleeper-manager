from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan
from pathlib import Path

import pytest
from test_d1 import FakeD1

from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import (
    ACKNOWLEDGEMENT_PROVENANCE,
    ACKNOWLEDGEMENT_TRACE_SCHEMA_VERSION,
    LOCK_IN_DECISION_TYPE,
    AcknowledgementQueryError,
    AcknowledgementRawRow,
    decode_acknowledged_decisions,
)
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
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


def test_canonical_constants_are_stable() -> None:
    assert LOCK_IN_DECISION_TYPE == "lock_in"
    assert ACKNOWLEDGEMENT_TRACE_SCHEMA_VERSION == 1
    assert ACKNOWLEDGEMENT_PROVENANCE == "repository_acknowledgement_v1"


def test_naive_as_of_is_rejected_before_rows_are_processed() -> None:
    with pytest.raises(AcknowledgementQueryError, match="timezone-aware"):
        decode_acknowledged_decisions((_row(),), as_of=datetime(2026, 1, 7, 18))


@pytest.mark.parametrize(
    "field,value",
    [
        ("recommendation_id", ""),
        ("recommendation_id", "  "),
        ("recommendation_id", None),
        ("player_id", ""),
        ("player_id", None),
        ("game_id", ""),
        ("game_id", " "),
        ("game_id", None),
    ],
)
def test_missing_identity_fields_are_storage_integrity_failures(
    field: str,
    value: object,
) -> None:
    with pytest.raises(AcknowledgementQueryError, match="identity"):
        decode_acknowledged_decisions((_row(**{field: value}),), as_of=AS_OF)


def test_unknown_authoritative_action_is_a_storage_integrity_failure() -> None:
    with pytest.raises(AcknowledgementQueryError, match="action"):
        decode_acknowledged_decisions((_row(acknowledgement_action="lock"),), as_of=AS_OF)


def test_naive_authoritative_timestamp_is_a_storage_integrity_failure() -> None:
    naive = "2026-01-07T18:00:00"
    with pytest.raises(AcknowledgementQueryError, match="timezone-aware"):
        decode_acknowledged_decisions((_row(acknowledged_at=naive),), as_of=AS_OF)


def test_invalid_authoritative_timestamp_is_a_storage_integrity_failure() -> None:
    with pytest.raises(AcknowledgementQueryError, match="timestamp"):
        decode_acknowledged_decisions((_row(acknowledged_at="not-a-time"),), as_of=AS_OF)


def test_point_in_time_includes_equivalent_offsets_and_excludes_newer() -> None:
    included_eastern = AS_OF.astimezone(EASTERN).isoformat()
    later = (AS_OF + timedelta(seconds=1)).isoformat()
    earlier = (AS_OF - timedelta(minutes=1)).isoformat()
    rows = (
        _row(
            recommendation_id="rec-later",
            acknowledged_at=later,
            recommendation_acknowledged_at=later,
        ),
        _row(
            recommendation_id="rec-boundary",
            acknowledged_at=included_eastern,
            recommendation_acknowledged_at=included_eastern,
        ),
        _row(
            recommendation_id="rec-earlier",
            acknowledged_at=earlier,
            recommendation_acknowledged_at=earlier,
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert [item.decision_id for item in result] == ["rec-earlier", "rec-boundary"]


def test_result_order_uses_datetime_comparison_not_iso_strings() -> None:
    later_utc = datetime(2026, 1, 7, 19, tzinfo=UTC)
    earlier_offset = datetime(2026, 1, 7, 12, tzinfo=timezone(timedelta(hours=-8)))
    rows = (
        _row(
            recommendation_id="rec-z",
            acknowledged_at=later_utc.isoformat(),
            recommendation_acknowledged_at=later_utc.isoformat(),
        ),
        _row(
            recommendation_id="rec-a",
            acknowledged_at=earlier_offset.isoformat(),
            recommendation_acknowledged_at=earlier_offset.isoformat(),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=datetime(2026, 1, 8, tzinfo=UTC))

    assert [item.decision_id for item in result] == ["rec-z", "rec-a"]
    assert result[0].decided_at == later_utc
    assert result[1].decided_at == earlier_offset


def test_canonical_lock_trace_decodes_to_reconciled_evidence() -> None:
    result = decode_acknowledged_decisions((_row(trace_json=_lock_trace()),), as_of=AS_OF)

    assert result == (
        AcknowledgedDecisionEvidence(
            decision_id="rec-1",
            player_id="p1",
            game_id="g1",
            action=AcknowledgedAction.LOCK,
            decided_at=AS_OF,
            provenance=ACKNOWLEDGEMENT_PROVENANCE,
            slot_index=2,
            slot_position="UTIL",
            accepted_fantasy_score=34.7,
            reconciled=True,
        ),
    )


def test_canonical_pass_trace_decodes_to_reconciled_evidence() -> None:
    row = _row(
        acknowledgement_action="passed",
        recommendation_action="passed",
        trace_json=_pass_trace(extra_top_level={"policy": {"ignored": True}}),
    )

    result = decode_acknowledged_decisions((row,), as_of=AS_OF)

    assert result == (
        AcknowledgedDecisionEvidence(
            decision_id="rec-1",
            player_id="p1",
            game_id="g1",
            action=AcknowledgedAction.PASS,
            decided_at=AS_OF,
            provenance=ACKNOWLEDGEMENT_PROVENANCE,
            reconciled=True,
        ),
    )


@pytest.mark.parametrize(
    "trace_json",
    ["{", "[1]", json.dumps({"acknowledgement": []}), json.dumps({"other": {}}), "null"],
)
def test_malformed_trace_yields_unreconciled_identity_bearing_evidence(trace_json: str) -> None:
    result = decode_acknowledged_decisions((_row(trace_json=trace_json),), as_of=AS_OF)

    assert len(result) == 1
    assert result[0].decision_id == "rec-1"
    assert result[0].action is AcknowledgedAction.LOCK
    assert result[0].reconciled is False
    assert result[0].slot_index is None
    assert result[0].slot_position is None
    assert result[0].accepted_fantasy_score is None


def test_unsupported_trace_version_is_unreconciled() -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(schema_version=2)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is False


@pytest.mark.parametrize("omit", [("slot_index",), ("slot_position",), ("accepted_fantasy_score",)])
def test_lock_missing_a_required_field_is_unreconciled(omit: tuple[str, ...]) -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(omit=omit)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is False
    if "slot_index" in omit:
        assert result[0].slot_index is None
    if "slot_position" in omit:
        assert result[0].slot_position is None
    if "accepted_fantasy_score" in omit:
        assert result[0].accepted_fantasy_score is None


@pytest.mark.parametrize("slot_index", [True, False, -1, 1.5, "2", None])
def test_invalid_slot_index_is_unreconciled(slot_index: object) -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(slot_index=slot_index)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is False
    assert result[0].slot_index is None


def test_non_negative_integer_slot_index_is_valid() -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(slot_index=0)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is True
    assert result[0].slot_index == 0


@pytest.mark.parametrize("slot_position", ["", "   "])
def test_blank_slot_position_is_unreconciled(slot_position: str) -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(slot_position=slot_position)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is False
    assert result[0].slot_position is None


def test_lowercase_slot_position_is_normalized() -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(slot_position="util")),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is True
    assert result[0].slot_position == "UTIL"


@pytest.mark.parametrize("score", [nan, inf, -inf, True, False, "34.7", None])
def test_invalid_accepted_score_is_unreconciled(score: object) -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(accepted_fantasy_score=score)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is False
    assert result[0].accepted_fantasy_score is None


@pytest.mark.parametrize("score", [0, 0.0, -3.5, 12])
def test_finite_accepted_scores_including_negatives_are_valid(score: float | int) -> None:
    result = decode_acknowledged_decisions(
        (_row(trace_json=_lock_trace(accepted_fantasy_score=score)),),
        as_of=AS_OF,
    )

    assert result[0].reconciled is True
    assert result[0].accepted_fantasy_score == float(score)


@pytest.mark.parametrize(
    "extra_fragment",
    [
        {"slot_index": 1},
        {"slot_position": "G"},
        {"accepted_fantasy_score": 12.0},
    ],
)
def test_pass_with_lock_only_members_is_unreconciled(extra_fragment: dict[str, object]) -> None:
    row = _row(
        acknowledgement_action="passed",
        recommendation_action="passed",
        trace_json=_pass_trace(extra_fragment=extra_fragment),
    )

    result = decode_acknowledged_decisions((row,), as_of=AS_OF)

    assert result[0].action is AcknowledgedAction.PASS
    assert result[0].reconciled is False
    assert result[0].slot_index is None
    assert result[0].slot_position is None
    assert result[0].accepted_fantasy_score is None


def test_duplicated_recommendation_fields_that_disagree_are_unreconciled() -> None:
    mismatched = _row(
        recommendation_status="pending",
        recommendation_action="passed",
        recommendation_acknowledged_at=(AS_OF + timedelta(minutes=5)).isoformat(),
    )

    result = decode_acknowledged_decisions((mismatched,), as_of=AS_OF)

    assert result[0].reconciled is False
    assert result[0].action is AcknowledgedAction.LOCK
    assert result[0].decided_at == AS_OF
    assert result[0].slot_index == 2


def test_decoder_never_returns_token_hashes() -> None:
    row = _row(
        trace_json=_lock_trace(extra_top_level={"token_hash": "secret"}),
    )

    dumped = repr(decode_acknowledged_decisions((row,), as_of=AS_OF))

    assert "secret" not in dumped
    assert "token_hash" not in dumped


def test_distinct_recommendation_ids_for_the_same_player_game_conflict() -> None:
    rows = (
        _row(recommendation_id="rec-a", trace_json=_lock_trace()),
        _row(
            recommendation_id="rec-b",
            acknowledgement_action="locked",
            recommendation_action="locked",
            trace_json=_lock_trace(slot_index=3, slot_position="G"),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert {item.decision_id for item in result} == {"rec-a", "rec-b"}
    assert all(item.reconciled is False for item in result)


def test_lock_and_pass_for_the_same_player_game_conflict() -> None:
    rows = (
        _row(recommendation_id="rec-lock"),
        _row(
            recommendation_id="rec-pass",
            acknowledgement_action="passed",
            recommendation_action="passed",
            trace_json=_pass_trace(),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert all(item.reconciled is False for item in result)


def test_two_locks_for_the_same_slot_conflict() -> None:
    rows = (
        _row(recommendation_id="rec-a", player_id="p1", game_id="g1", trace_json=_lock_trace()),
        _row(
            recommendation_id="rec-b",
            player_id="p2",
            game_id="g2",
            trace_json=_lock_trace(),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert all(item.reconciled is False for item in result)


def test_two_locks_for_the_same_player_conflict() -> None:
    rows = (
        _row(recommendation_id="rec-a", game_id="g1", trace_json=_lock_trace(slot_index=0)),
        _row(
            recommendation_id="rec-b",
            game_id="g2",
            trace_json=_lock_trace(slot_index=1, slot_position="G"),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert all(item.reconciled is False for item in result)


def test_passes_for_different_games_and_later_lock_are_valid() -> None:
    earlier = (AS_OF - timedelta(hours=2)).isoformat()
    later = (AS_OF - timedelta(hours=1)).isoformat()
    rows = (
        _row(
            recommendation_id="rec-pass-g1",
            game_id="g1",
            acknowledgement_action="passed",
            recommendation_action="passed",
            acknowledged_at=earlier,
            recommendation_acknowledged_at=earlier,
            trace_json=_pass_trace(),
        ),
        _row(
            recommendation_id="rec-pass-g2",
            game_id="g2",
            acknowledgement_action="passed",
            recommendation_action="passed",
            acknowledged_at=earlier,
            recommendation_acknowledged_at=earlier,
            trace_json=_pass_trace(),
        ),
        _row(
            recommendation_id="rec-lock-g3",
            game_id="g3",
            acknowledged_at=later,
            recommendation_acknowledged_at=later,
            trace_json=_lock_trace(),
        ),
    )

    result = decode_acknowledged_decisions(rows, as_of=AS_OF)

    assert [item.reconciled for item in result] == [True, True, True]
    assert [item.action for item in result] == [
        AcknowledgedAction.PASS,
        AcknowledgedAction.PASS,
        AcknowledgedAction.LOCK,
    ]


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


@pytest.fixture(params=["sqlite", "d1"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    return _Backend(request.param, tmp_path)


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


def test_migration_adds_index_without_rewriting_phase3_rows(tmp_path: Path) -> None:
    path = tmp_path / "migrated.db"
    connection = sqlite3.connect(path)
    root = Path(__file__).resolve().parents[2]
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
