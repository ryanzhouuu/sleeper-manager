"""Deterministically encode normalized forecast records for SQLite and D1."""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    NormalizedPlayerForecast,
)

FORECAST_SNAPSHOT_SCHEMA_VERSION = "normalized-forecast-records-v1"
_SNAPSHOT_FIELDS = frozenset(("schema_version", "records"))
_RECORD_FIELDS = frozenset(("player_id", "company", "team_id", "provider_updated_at", "stats"))


class ForecastSnapshotCodecError(ForecastCaptureError):
    """Raised when normalized snapshot bytes violate their storage contract."""


@dataclass(frozen=True, slots=True)
class EncodedForecastSnapshot:
    """Carries one compressed normalized record surface and its decoded size."""

    encoding: ForecastArtifactEncoding
    encoded_records: bytes
    uncompressed_size: int

    def __post_init__(self) -> None:
        """Reject unsupported, empty, or size-less storage envelopes."""

        if self.encoding is not ForecastArtifactEncoding.GZIP_JSON:
            raise ForecastSnapshotCodecError("Forecast snapshot encoding is unsupported")
        if not isinstance(self.encoded_records, bytes) or not self.encoded_records:
            raise ForecastSnapshotCodecError("Forecast snapshot bytes must be non-empty")
        if (
            isinstance(self.uncompressed_size, bool)
            or not isinstance(self.uncompressed_size, int)
            or self.uncompressed_size <= 0
        ):
            raise ForecastSnapshotCodecError("Forecast snapshot size must be positive")


def encode_forecast_snapshot(
    records: Sequence[NormalizedPlayerForecast],
) -> EncodedForecastSnapshot:
    """Serialize one player surface with stable ordering, JSON, and gzip metadata."""

    supplied = tuple(records)
    if any(not isinstance(record, NormalizedPlayerForecast) for record in supplied):
        raise ForecastSnapshotCodecError("Forecast snapshot records must be normalized")
    ordered = tuple(sorted(supplied, key=lambda record: record.player_id))
    player_ids = tuple(record.player_id for record in ordered)
    if len(set(player_ids)) != len(player_ids):
        raise ForecastSnapshotCodecError("Forecast snapshot contains duplicate player IDs")

    document = {
        "schema_version": FORECAST_SNAPSHOT_SCHEMA_VERSION,
        "records": [_record_document(record) for record in ordered],
    }
    try:
        payload = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ForecastSnapshotCodecError("Forecast snapshot cannot be encoded") from error
    return EncodedForecastSnapshot(
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_records=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
    )


def decode_forecast_snapshot(
    snapshot: EncodedForecastSnapshot,
) -> tuple[NormalizedPlayerForecast, ...]:
    """Strictly recover normalized records from a versioned compressed snapshot."""

    try:
        payload = gzip.decompress(snapshot.encoded_records)
    except (EOFError, OSError) as error:
        raise ForecastSnapshotCodecError(
            "Forecast snapshot must contain valid gzip data"
        ) from error
    if len(payload) != snapshot.uncompressed_size:
        raise ForecastSnapshotCodecError("Forecast snapshot uncompressed size does not match")

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ForecastSnapshotCodecError(
            "Forecast snapshot must contain valid UTF-8 JSON"
        ) from error
    document = _mapping(decoded, "Forecast snapshot")
    _require_exact_fields(document, _SNAPSHOT_FIELDS, "Forecast snapshot")
    if document["schema_version"] != FORECAST_SNAPSHOT_SCHEMA_VERSION:
        raise ForecastSnapshotCodecError("Forecast snapshot schema version is unsupported")
    raw_records = document["records"]
    if not isinstance(raw_records, list):
        raise ForecastSnapshotCodecError("Forecast snapshot records must be an array")

    records = tuple(_decode_record(item, index=index) for index, item in enumerate(raw_records))
    player_ids = tuple(record.player_id for record in records)
    if len(set(player_ids)) != len(player_ids):
        raise ForecastSnapshotCodecError("Forecast snapshot contains duplicate player IDs")
    return tuple(sorted(records, key=lambda record: record.player_id))


def _record_document(record: NormalizedPlayerForecast) -> dict[str, object]:
    """Convert one validated record into the stable snapshot field set."""

    return {
        "player_id": record.player_id,
        "company": record.company,
        "team_id": record.team_id,
        "provider_updated_at": (
            _utc_timestamp(record.provider_updated_at)
            if record.provider_updated_at is not None
            else None
        ),
        "stats": dict(record.stats),
    }


def _decode_record(value: object, *, index: int) -> NormalizedPlayerForecast:
    """Decode one strict record and reapply the domain invariants."""

    label = f"Forecast snapshot record {index}"
    record = _mapping(value, label)
    _require_exact_fields(record, _RECORD_FIELDS, label)
    player_id = _text(record["player_id"], f"{label} player_id")
    company = _text(record["company"], f"{label} company")
    team_value = record["team_id"]
    team_id = None if team_value is None else _text(team_value, f"{label} team_id")
    updated_at = _decode_timestamp(record["provider_updated_at"], label)
    stats = _mapping(record["stats"], f"{label} stats")
    decoded_stats: list[tuple[str, float]] = []
    for name, stat_value in stats.items():
        if (
            isinstance(stat_value, bool)
            or not isinstance(stat_value, int | float)
            or not isfinite(stat_value)
        ):
            raise ForecastSnapshotCodecError(f"{label} stat {name!r} must be a finite number")
        decoded_stats.append((name, float(stat_value)))
    try:
        return NormalizedPlayerForecast(
            player_id=player_id,
            company=company,
            team_id=team_id,
            provider_updated_at=updated_at,
            stats=tuple(decoded_stats),
        )
    except ForecastCaptureError as error:
        raise ForecastSnapshotCodecError(str(error)) from error


def _decode_timestamp(value: object, label: str) -> datetime | None:
    """Require the codec's canonical UTC timestamp representation."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ForecastSnapshotCodecError(f"{label} provider update must be a UTC timestamp")
    try:
        timestamp = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ForecastSnapshotCodecError(
            f"{label} provider update must be a UTC timestamp"
        ) from error
    if _utc_timestamp(timestamp) != value:
        raise ForecastSnapshotCodecError(f"{label} provider update must be a UTC timestamp")
    return timestamp


def _utc_timestamp(value: datetime) -> str:
    """Render one aware timestamp in the codec's canonical UTC form."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ForecastSnapshotCodecError("Forecast snapshot timestamps must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: object, label: str) -> dict[str, Any]:
    """Require a concrete JSON object with string keys."""

    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ForecastSnapshotCodecError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _text(value: object, label: str) -> str:
    """Require a non-empty string before invoking domain validation."""

    if not isinstance(value, str) or not value.strip():
        raise ForecastSnapshotCodecError(f"{label} must be non-empty text")
    return value


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    """Reject omitted or future fields unless a schema version explicitly admits them."""

    actual = frozenset(value)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        unexpected = ", ".join(sorted(actual - expected)) or "none"
        raise ForecastSnapshotCodecError(
            f"{label} fields are incompatible; missing: {missing}; unexpected: {unexpected}"
        )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject repeated object names instead of accepting parser-order semantics."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ForecastSnapshotCodecError(f"Forecast snapshot duplicate field {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject nonstandard NaN and infinity tokens before domain construction."""

    raise ForecastSnapshotCodecError(f"Forecast snapshot contains invalid constant {value!r}")


__all__ = (
    "FORECAST_SNAPSHOT_SCHEMA_VERSION",
    "EncodedForecastSnapshot",
    "ForecastSnapshotCodecError",
    "decode_forecast_snapshot",
    "encode_forecast_snapshot",
)
