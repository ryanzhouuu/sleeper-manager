"""Strictly normalize Sleeper season forecasts into capture evidence.

The undocumented source payload remains available as a compressed raw artifact.
Only qualified identity and numeric forecast fields enter the semantic revision;
provider timestamps and mutable player metadata remain provenance.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCoverage,
    ForecastSource,
    NormalizedForecastRevision,
    NormalizedPlayerForecast,
    RawForecastArtifact,
)

CORE_FORECAST_STATS = frozenset(("pts", "reb", "ast", "stl", "blk", "to", "tpm"))
_REQUIRED_ROW_FIELDS = frozenset(
    ("player_id", "company", "sport", "category", "season", "season_type", "game_id", "stats")
)


class SleeperForecastPayloadError(ForecastCaptureError):
    """Raised when the Sleeper response cannot produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class ParsedSleeperForecastCapture:
    """Pairs an exact source artifact with its deduplicated semantic candidate."""

    artifact: RawForecastArtifact
    revision: NormalizedForecastRevision


def parse_sleeper_season_forecasts(
    payload: bytes,
    *,
    source: ForecastSource,
    persisted_at: datetime,
) -> ParsedSleeperForecastCapture:
    """Parse one complete season-feed response without inferring missing values."""

    _validate_source(source)
    if not isinstance(payload, bytes) or not payload:
        raise SleeperForecastPayloadError("Sleeper forecast payload must be non-empty bytes")
    decoded = _decode_payload(payload)
    if not isinstance(decoded, list):
        raise SleeperForecastPayloadError("Sleeper forecast payload root must be an array")

    records = tuple(
        _parse_row(item, index=index, source=source) for index, item in enumerate(decoded)
    )
    player_ids = tuple(record.player_id for record in records)
    if len(set(player_ids)) != len(player_ids):
        raise SleeperForecastPayloadError("Sleeper forecast payload contains duplicate player IDs")

    ordered = tuple(sorted(records, key=lambda record: record.player_id))
    payload_hash = sha256(payload).hexdigest()
    semantic_hash = sha256(_semantic_payload(source, ordered)).hexdigest()
    updates = tuple(
        record.provider_updated_at for record in ordered if record.provider_updated_at is not None
    )
    coverage = ForecastCoverage(
        total_rows=len(ordered),
        numeric_forecast_rows=sum(record.stat("pts") is not None for record in ordered),
        core_complete_rows=sum(
            CORE_FORECAST_STATS.issubset(name for name, _ in record.stats) for record in ordered
        ),
    )
    artifact = RawForecastArtifact(
        payload_hash=payload_hash,
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
        stored_at=persisted_at,
    )
    revision = NormalizedForecastRevision(
        revision_id=f"{source.adapter_version}:{semantic_hash}",
        source=source,
        semantic_hash=semantic_hash,
        source_payload_hash=payload_hash,
        first_persisted_at=persisted_at,
        coverage=coverage,
        records=ordered,
        provider_updated_from=min(updates) if updates else None,
        provider_updated_to=max(updates) if updates else None,
    )
    return ParsedSleeperForecastCapture(artifact=artifact, revision=revision)


def _validate_source(source: ForecastSource) -> None:
    """Restrict this adapter to the qualified Sleeper season surface."""

    if source.provider != "sleeper":
        raise SleeperForecastPayloadError("Sleeper forecast adapter requires provider 'sleeper'")
    if source.horizon != "season":
        raise SleeperForecastPayloadError("Sleeper forecast adapter supports only season horizon")


def _decode_payload(payload: bytes) -> object:
    """Decode strict JSON, rejecting invalid constants and duplicate object fields."""

    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SleeperForecastPayloadError(
            "Sleeper forecast payload must be valid UTF-8 JSON"
        ) from error


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys so source values cannot be parser-order dependent."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SleeperForecastPayloadError(f"Sleeper forecast JSON field {key!r} is duplicated")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject nonstandard NaN and infinity tokens before numeric normalization."""

    raise SleeperForecastPayloadError(f"Sleeper forecast JSON constant {value!r} is invalid")


def _parse_row(
    value: object,
    *,
    index: int,
    source: ForecastSource,
) -> NormalizedPlayerForecast:
    """Validate one source row and retain every supplied numeric stat."""

    row = _require_object(value, f"Sleeper forecast row {index}")
    missing = _REQUIRED_ROW_FIELDS - row.keys()
    if missing:
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} is missing fields: {', '.join(sorted(missing))}"
        )
    _require_literal(row, "sport", "nba", index)
    _require_literal(row, "category", "proj", index)
    _require_literal(row, "season", source.season, index)
    _require_literal(row, "season_type", source.season_type, index)
    _require_literal(row, "game_id", source.horizon, index)

    return NormalizedPlayerForecast(
        player_id=_require_string(row, "player_id", index),
        company=_require_string(row, "company", index),
        team_id=_optional_string(row.get("team"), "team", index),
        provider_updated_at=_provider_timestamp(row.get("updated_at"), index),
        stats=_numeric_stats(row["stats"], index),
    )


def _require_object(value: object, label: str) -> dict[str, object]:
    """Return a JSON object with string keys or fail with source context."""

    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SleeperForecastPayloadError(f"{label} must be an object")
    return value


def _require_string(row: Mapping[str, object], field: str, index: int) -> str:
    """Read one required nonblank source string without coercion."""

    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} field {field!r} must be a non-empty string"
        )
    return value


def _optional_string(value: object, field: str, index: int) -> str | None:
    """Read a nullable source string while rejecting alternate shapes."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} field {field!r} must be null or a non-empty string"
        )
    return value


def _require_literal(row: Mapping[str, object], field: str, expected: str, index: int) -> None:
    """Bind every row to the requested sport, season, and horizon."""

    if row[field] != expected:
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} field {field!r} must equal {expected!r}"
        )


def _provider_timestamp(value: object, index: int) -> datetime | None:
    """Decode Sleeper's nullable Unix-millisecond update timestamp."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} field 'updated_at' must be positive milliseconds or null"
        )
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise SleeperForecastPayloadError(
            f"Sleeper forecast row {index} field 'updated_at' is outside the supported range"
        ) from error


def _numeric_stats(value: object, index: int) -> tuple[tuple[str, float], ...]:
    """Retain numeric stat fields exactly while rejecting silent coercion."""

    stats = _require_object(value, f"Sleeper forecast row {index} field 'stats'")
    normalized: list[tuple[str, float]] = []
    for name, raw_value in stats.items():
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int | float)
            or not isfinite(raw_value)
        ):
            raise SleeperForecastPayloadError(
                f"Sleeper forecast row {index} stat {name!r} must be a finite number"
            )
        normalized.append((name, float(raw_value)))
    return tuple(normalized)


def _semantic_payload(
    source: ForecastSource,
    records: tuple[NormalizedPlayerForecast, ...],
) -> bytes:
    """Serialize only fields whose changes create a forecast revision."""

    value = {
        "source": {
            "provider": source.provider,
            "endpoint": source.endpoint,
            "season": source.season,
            "season_type": source.season_type,
            "horizon": source.horizon,
            "adapter_version": source.adapter_version,
        },
        "records": [
            {
                "player_id": record.player_id,
                "company": record.company,
                "team_id": record.team_id,
                "stats": record.stats,
            }
            for record in records
        ],
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


__all__ = (
    "CORE_FORECAST_STATS",
    "ParsedSleeperForecastCapture",
    "SleeperForecastPayloadError",
    "parse_sleeper_season_forecasts",
)
