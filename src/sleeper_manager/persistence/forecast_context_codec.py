"""Encode one capture-context snapshot for the forecast archive."""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sleeper_manager.domain._forecast_capture_validation import require_sha256
from sleeper_manager.domain.forecast_capture import ForecastArtifactEncoding, ForecastCaptureError
from sleeper_manager.domain.forecast_context import (
    ForecastCaptureContext,
    ForecastContextEligibility,
    ForecastContextGame,
    ForecastContextGap,
    ForecastContextGapCode,
    ForecastContextRoster,
)
from sleeper_manager.domain.nba import GameStatus

FORECAST_CONTEXT_SCHEMA_VERSION = "forecast-capture-context-v1"


class ForecastContextCodecError(ForecastCaptureError):
    """Raised when stored companion evidence cannot be decoded."""


@dataclass(frozen=True, slots=True)
class EncodedForecastContext:
    """Compressed companion snapshot addressed by its uncompressed hash."""

    receipt_id: str
    encoding: ForecastArtifactEncoding
    encoded_payload: bytes
    uncompressed_size: int
    content_hash: str
    stored_at: datetime

    def __post_init__(self) -> None:
        if not self.receipt_id.strip():
            raise ForecastContextCodecError("Forecast context receipt ID must be non-empty")
        if self.encoding is not ForecastArtifactEncoding.GZIP_JSON:
            raise ForecastContextCodecError("Forecast context encoding is unsupported")
        if not self.encoded_payload:
            raise ForecastContextCodecError("Forecast context payload must not be empty")
        if (
            isinstance(self.uncompressed_size, bool)
            or not isinstance(self.uncompressed_size, int)
            or self.uncompressed_size <= 0
        ):
            raise ForecastContextCodecError("Forecast context size must be positive")
        require_sha256(self.content_hash, "Forecast context hash")


def encoded_context_from_mapping(row: Mapping[str, Any]) -> EncodedForecastContext:
    """Build one stored envelope from a repository row."""

    try:
        encoding = ForecastArtifactEncoding(str(row["encoding"]))
        stored_at = datetime.fromisoformat(str(row["stored_at"]))
        payload = row["encoded_payload"]
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        return EncodedForecastContext(
            receipt_id=str(row["receipt_id"]),
            encoding=encoding,
            encoded_payload=payload,
            uncompressed_size=int(row["uncompressed_size"]),
            content_hash=str(row["content_hash"]),
            stored_at=stored_at,
        )
    except (KeyError, TypeError, ValueError, ForecastCaptureError) as error:
        raise ForecastContextCodecError("Stored forecast context row is invalid") from error


def encode_forecast_context(context: ForecastCaptureContext) -> EncodedForecastContext:
    """Serialize companion evidence with stable JSON and gzip metadata."""

    payload = _dumps(_document(context))
    return EncodedForecastContext(
        receipt_id=context.receipt_id,
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
        content_hash=sha256(payload).hexdigest(),
        stored_at=context.persisted_at,
    )


def decode_forecast_context(encoded: EncodedForecastContext) -> ForecastCaptureContext:
    """Restore one snapshot after checking gzip, size, and content hash."""

    try:
        payload = gzip.decompress(encoded.encoded_payload)
    except (EOFError, OSError) as error:
        raise ForecastContextCodecError("Forecast context payload is corrupt") from error
    if (
        len(payload) != encoded.uncompressed_size
        or sha256(payload).hexdigest() != encoded.content_hash
    ):
        raise ForecastContextCodecError("Forecast context identity is corrupt")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ForecastContextCodecError("Forecast context payload is not JSON") from error
    context = _context(document)
    if context.receipt_id != encoded.receipt_id or context.persisted_at != encoded.stored_at:
        raise ForecastContextCodecError("Forecast context envelope disagrees with its payload")
    return context


def same_forecast_context(left: ForecastCaptureContext, right: ForecastCaptureContext) -> bool:
    """Compare companion snapshots by their canonical uncompressed bytes."""

    return encode_forecast_context(left).content_hash == encode_forecast_context(right).content_hash


def _document(context: ForecastCaptureContext) -> dict[str, Any]:
    return {
        "schema_version": FORECAST_CONTEXT_SCHEMA_VERSION,
        "receipt_id": context.receipt_id,
        "league_id": context.league_id,
        "season": context.season,
        "week": context.week,
        "matchup_id": context.matchup_id,
        "bye": context.bye,
        "manager": _roster_document(context.manager),
        "opponent": _roster_document(context.opponent),
        "eligibility": [_eligibility_document(item) for item in context.eligibility],
        "games": [_game_document(item) for item in context.games],
        "gaps": [_gap_document(item) for item in context.gaps],
        "persisted_at": context.persisted_at.isoformat(),
    }


def _roster_document(roster: ForecastContextRoster | None) -> dict[str, Any] | None:
    if roster is None:
        return None
    return {
        "roster_id": roster.roster_id,
        "player_ids": list(roster.player_ids),
        "starter_ids": list(roster.starter_ids),
        "reserve_ids": list(roster.reserve_ids),
        "read_at": roster.read_at.isoformat(),
    }


def _eligibility_document(item: ForecastContextEligibility) -> dict[str, Any]:
    return {
        "player_id": item.player_id,
        "positions": list(item.positions),
        "read_at": item.read_at.isoformat(),
    }


def _game_document(item: ForecastContextGame) -> dict[str, Any]:
    return {
        "game_id": item.game_id,
        "home_team_id": item.home_team_id,
        "away_team_id": item.away_team_id,
        "start_time": item.start_time.isoformat(),
        "status": item.status.value,
        "read_at": item.read_at.isoformat(),
    }


def _gap_document(item: ForecastContextGap) -> dict[str, Any]:
    return {"code": item.code.value, "subject": item.subject, "detail": item.detail}


def _context(document: object) -> ForecastCaptureContext:
    if not isinstance(document, dict):
        raise ForecastContextCodecError("Forecast context payload must be an object")
    if document.get("schema_version") != FORECAST_CONTEXT_SCHEMA_VERSION:
        raise ForecastContextCodecError("Forecast context schema version is unsupported")
    try:
        return ForecastCaptureContext(
            receipt_id=_text(document, "receipt_id"),
            league_id=_text(document, "league_id"),
            season=_text(document, "season"),
            week=_int(document, "week"),
            matchup_id=_optional_int(document, "matchup_id"),
            bye=_bool(document, "bye"),
            manager=_roster(document.get("manager")),
            opponent=_roster(document.get("opponent")),
            eligibility=tuple(_eligibility(item) for item in _list(document, "eligibility")),
            games=tuple(_game(item) for item in _list(document, "games")),
            gaps=tuple(_gap(item) for item in _list(document, "gaps")),
            persisted_at=_time(_text(document, "persisted_at"), "persistence time"),
        )
    except ForecastCaptureError as error:
        raise ForecastContextCodecError(str(error)) from error


def _roster(value: object) -> ForecastContextRoster | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ForecastContextCodecError("Forecast context roster must be an object")
    starters = value.get("starter_ids")
    if not isinstance(starters, list):
        raise ForecastContextCodecError("Forecast context starters must be a list")
    return ForecastContextRoster(
        roster_id=_int(value, "roster_id"),
        player_ids=tuple(_text_item(item, "player") for item in _list(value, "player_ids")),
        starter_ids=tuple(_optional_text_item(item) for item in starters),
        reserve_ids=tuple(_text_item(item, "player") for item in _list(value, "reserve_ids")),
        read_at=_time(_text(value, "read_at"), "roster read"),
    )


def _eligibility(value: object) -> ForecastContextEligibility:
    if not isinstance(value, dict):
        raise ForecastContextCodecError("Forecast context eligibility must be an object")
    return ForecastContextEligibility(
        player_id=_text(value, "player_id"),
        positions=tuple(_text_item(item, "position") for item in _list(value, "positions")),
        read_at=_time(_text(value, "read_at"), "eligibility read"),
    )


def _game(value: object) -> ForecastContextGame:
    if not isinstance(value, dict):
        raise ForecastContextCodecError("Forecast context game must be an object")
    try:
        status = GameStatus(_text(value, "status"))
    except ValueError as error:
        raise ForecastContextCodecError("Forecast context game status is unsupported") from error
    return ForecastContextGame(
        game_id=_text(value, "game_id"),
        home_team_id=_text(value, "home_team_id"),
        away_team_id=_text(value, "away_team_id"),
        start_time=_time(_text(value, "start_time"), "game start"),
        status=status,
        read_at=_time(_text(value, "read_at"), "game read"),
    )


def _gap(value: object) -> ForecastContextGap:
    if not isinstance(value, dict):
        raise ForecastContextCodecError("Forecast context gap must be an object")
    try:
        code = ForecastContextGapCode(_text(value, "code"))
    except ValueError as error:
        raise ForecastContextCodecError("Forecast context gap code is unsupported") from error
    return ForecastContextGap(
        code=code,
        subject=_text(value, "subject"),
        detail=_text(value, "detail"),
    )


def _dumps(document: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ForecastContextCodecError("Forecast context cannot be encoded") from error


def _list(document: dict[str, Any], key: str) -> list[object]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ForecastContextCodecError(f"Forecast context {key} must be a list")
    return value


def _text(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise ForecastContextCodecError(f"Forecast context {key} must be text")
    return value


def _text_item(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ForecastContextCodecError(f"Forecast context {label} must be text")
    return value


def _optional_text_item(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ForecastContextCodecError("Forecast context starter must be text")
    return value


def _int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ForecastContextCodecError(f"Forecast context {key} must be an integer")
    return value


def _optional_int(document: dict[str, Any], key: str) -> int | None:
    if document.get(key) is None:
        return None
    return _int(document, key)


def _bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ForecastContextCodecError(f"Forecast context {key} must be boolean")
    return value


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ForecastContextCodecError(f"Forecast context {label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForecastContextCodecError(f"Forecast context {label} must be timezone-aware")
    return parsed


__all__ = (
    "EncodedForecastContext",
    "ForecastContextCodecError",
    "decode_forecast_context",
    "encode_forecast_context",
    "encoded_context_from_mapping",
    "same_forecast_context",
)
