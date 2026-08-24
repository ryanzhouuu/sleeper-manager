from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Any

from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence

LOCK_IN_DECISION_TYPE = "lock_in"
ACKNOWLEDGEMENT_TRACE_SCHEMA_VERSION = 1
ACKNOWLEDGEMENT_PROVENANCE = "repository_acknowledgement_v1"

ACKNOWLEDGED_DECISIONS_QUERY = """
SELECT
    r.recommendation_id AS recommendation_id,
    r.player_id AS player_id,
    r.game_id AS game_id,
    r.status AS recommendation_status,
    r.acknowledged_action AS recommendation_action,
    r.acknowledged_at AS recommendation_acknowledged_at,
    r.trace_json AS trace_json,
    a.action AS acknowledgement_action,
    a.acknowledged_at AS acknowledged_at
FROM recommendations r
JOIN acknowledgements a ON a.recommendation_id = r.recommendation_id
WHERE r.league_id = ?
  AND r.fantasy_week = ?
  AND r.decision_type = ?
"""

ACKNOWLEDGED_DECISIONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS recommendations_league_week_decision_status_idx
ON recommendations (league_id, fantasy_week, decision_type, status)
"""

_LOCK_FRAGMENT_FIELDS = ("slot_index", "slot_position", "accepted_fantasy_score")
_ACTION_BY_STORED_VALUE = {
    "locked": AcknowledgedAction.LOCK,
    "passed": AcknowledgedAction.PASS,
}


class AcknowledgementQueryError(RuntimeError):
    """Raised when stored acknowledgement rows cannot form identity-bearing evidence."""


@dataclass(frozen=True, slots=True)
class AcknowledgementRawRow:
    recommendation_id: object
    player_id: object
    game_id: object
    recommendation_status: object
    recommendation_action: object
    recommendation_acknowledged_at: object
    trace_json: object
    acknowledgement_action: object
    acknowledged_at: object


def raw_row_from_mapping(row: Mapping[str, Any]) -> AcknowledgementRawRow:
    return AcknowledgementRawRow(
        recommendation_id=row.get("recommendation_id"),
        player_id=row.get("player_id"),
        game_id=row.get("game_id"),
        recommendation_status=row.get("recommendation_status"),
        recommendation_action=row.get("recommendation_action"),
        recommendation_acknowledged_at=row.get("recommendation_acknowledged_at"),
        trace_json=row.get("trace_json"),
        acknowledgement_action=row.get("acknowledgement_action"),
        acknowledged_at=row.get("acknowledged_at"),
    )


def raw_row_from_sequence(row: Sequence[object]) -> AcknowledgementRawRow:
    return AcknowledgementRawRow(
        recommendation_id=row[0],
        player_id=row[1],
        game_id=row[2],
        recommendation_status=row[3],
        recommendation_action=row[4],
        recommendation_acknowledged_at=row[5],
        trace_json=row[6],
        acknowledgement_action=row[7],
        acknowledged_at=row[8],
    )


def decode_acknowledged_decisions(
    rows: Sequence[AcknowledgementRawRow],
    *,
    as_of: datetime,
) -> tuple[AcknowledgedDecisionEvidence, ...]:
    if as_of.tzinfo is None:
        raise AcknowledgementQueryError("as_of must be timezone-aware")
    decoded: list[AcknowledgedDecisionEvidence] = []
    for row in rows:
        decided_at = _parse_aware(row.acknowledged_at, label="acknowledgement timestamp")
        if decided_at > as_of:
            continue
        decoded.append(_decode_row(row, decided_at))
    return _reconcile(_order(decoded))


def _decode_row(
    row: AcknowledgementRawRow,
    decided_at: datetime,
) -> AcknowledgedDecisionEvidence:
    decision_id = _require_identity(row.recommendation_id, "recommendation ID")
    player_id = _require_identity(row.player_id, "player ID")
    game_id = _require_identity(row.game_id, "game ID")
    action = _ACTION_BY_STORED_VALUE.get(_text_or_none(row.acknowledgement_action) or "")
    if action is None:
        raise AcknowledgementQueryError("unknown acknowledgement action")
    slot_index, slot_position, accepted_score, trace_ok = _parse_trace(row.trace_json, action)
    duplicates_ok = _duplicates_match(row, action, decided_at)
    return AcknowledgedDecisionEvidence(
        decision_id=decision_id,
        player_id=player_id,
        game_id=game_id,
        action=action,
        decided_at=decided_at,
        provenance=ACKNOWLEDGEMENT_PROVENANCE,
        slot_index=slot_index,
        slot_position=slot_position,
        accepted_fantasy_score=accepted_score,
        reconciled=trace_ok and duplicates_ok,
    )


def _duplicates_match(
    row: AcknowledgementRawRow,
    action: AcknowledgedAction,
    decided_at: datetime,
) -> bool:
    status = _text_or_none(row.recommendation_status)
    stored_action = _ACTION_BY_STORED_VALUE.get(_text_or_none(row.recommendation_action) or "")
    try:
        stored_at = _parse_aware(
            row.recommendation_acknowledged_at,
            label="recommendation acknowledgement timestamp",
        )
    except AcknowledgementQueryError:
        return False
    return status == "acknowledged" and stored_action is action and stored_at == decided_at


def _parse_trace(
    trace_json: object,
    action: AcknowledgedAction,
) -> tuple[int | None, str | None, float | None, bool]:
    if not isinstance(trace_json, str):
        return None, None, None, False
    try:
        payload = json.loads(trace_json)
    except json.JSONDecodeError:
        return None, None, None, False
    if not isinstance(payload, dict):
        return None, None, None, False
    fragment = payload.get("acknowledgement")
    if not isinstance(fragment, dict):
        return None, None, None, False
    if fragment.get("schema_version") != ACKNOWLEDGEMENT_TRACE_SCHEMA_VERSION or isinstance(
        fragment.get("schema_version"), bool
    ):
        return None, None, None, False
    slot_index = _parse_slot_index(fragment.get("slot_index"))
    slot_position = _parse_slot_position(fragment.get("slot_position"))
    accepted_score = _parse_accepted_score(fragment.get("accepted_fantasy_score"))
    has_lock_member = any(key in fragment for key in _LOCK_FRAGMENT_FIELDS)
    if action is AcknowledgedAction.PASS:
        return None, None, None, not has_lock_member
    complete = slot_index is not None and slot_position is not None and accepted_score is not None
    return slot_index, slot_position, accepted_score, complete


def _parse_slot_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_slot_position(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper()


def _parse_accepted_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    score = float(value)
    if not isfinite(score):
        return None
    return score


def _require_identity(value: object, label: str) -> str:
    text = _text_or_none(value)
    if text is None:
        raise AcknowledgementQueryError(f"acknowledgement identity is missing {label}")
    return text


def _text_or_none(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _parse_aware(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AcknowledgementQueryError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AcknowledgementQueryError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise AcknowledgementQueryError(f"{label} must be timezone-aware")
    return parsed


def _order(
    evidence: Sequence[AcknowledgedDecisionEvidence],
) -> tuple[AcknowledgedDecisionEvidence, ...]:
    return tuple(sorted(evidence, key=lambda item: (item.decided_at, item.decision_id)))


def _reconcile(
    evidence: Sequence[AcknowledgedDecisionEvidence],
) -> tuple[AcknowledgedDecisionEvidence, ...]:
    player_games: dict[tuple[str, str], set[str]] = defaultdict(set)
    locked_slots: dict[int, set[str]] = defaultdict(set)
    locked_players: dict[str, set[str]] = defaultdict(set)
    for item in evidence:
        player_games[item.player_id, item.game_id].add(item.decision_id)
        if item.action is AcknowledgedAction.LOCK:
            locked_players[item.player_id].add(item.decision_id)
            if item.slot_index is not None:
                locked_slots[item.slot_index].add(item.decision_id)
    conflicted: set[str] = set()
    for ids in (*player_games.values(), *locked_slots.values(), *locked_players.values()):
        if len(ids) > 1:
            conflicted.update(ids)
    return tuple(
        replace(item, reconciled=False) if item.decision_id in conflicted else item
        for item in evidence
    )
