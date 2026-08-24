from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan

import pytest

from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import (
    ACKNOWLEDGEMENT_PROVENANCE,
    ACKNOWLEDGEMENT_TRACE_SCHEMA_VERSION,
    LOCK_IN_DECISION_TYPE,
    AcknowledgementQueryError,
    AcknowledgementRawRow,
    decode_acknowledged_decisions,
)

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
