"""Strict JSON boundary for immutable historical projection sidecars.

This module owns wire-shape validation and immutable writes. Cross-record
coverage and team-week compatibility remain in ``projection_surface``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.artifacts import atomic_write_bytes, canonical_json_bytes
from sleeper_manager.backtesting.replay.inputs._artifact_decoding import (
    decode_projection_snapshot,
)
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    SourceFingerprint,
)
from sleeper_manager.backtesting.replay.projection_surface import (
    validate_historical_projection_surface,
)
from sleeper_manager.backtesting.replay.projection_surface_models import (
    HistoricalProjectionSurface,
    HistoricalProjectionSurfaceError,
    ProjectionSurfaceEntry,
)


def write_historical_projection_surface_artifact(
    path: Path, surface: HistoricalProjectionSurface
) -> str:
    """Write an immutable canonical sidecar and return its logical fingerprint."""

    encoded = canonical_json_bytes(surface.to_dict())
    if path.exists():
        if path.read_bytes() != encoded:
            raise HistoricalProjectionSurfaceError(
                f"Refusing to overwrite immutable projection surface: {path}"
            )
    else:
        atomic_write_bytes(path, encoded)
    return surface.fingerprint


def load_historical_projection_surface_artifact(
    path: Path,
    *,
    team_week: HistoricalTeamWeekInput,
    projection_config_version: str | None = None,
) -> HistoricalProjectionSurface:
    """Decode and validate a sidecar against its selected historical team-week."""

    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalProjectionSurfaceError(
            f"Unable to read projection surface: {path}"
        ) from error
    surface, persisted_fingerprint = _decode_surface(payload)
    if persisted_fingerprint != surface.fingerprint:
        raise HistoricalProjectionSurfaceError("Projection-surface fingerprint mismatch")
    validate_historical_projection_surface(
        surface,
        team_week=team_week,
        projection_config_version=projection_config_version,
    )
    return surface


def _decode_surface(payload: object) -> tuple[HistoricalProjectionSurface, str]:
    """Decode the strict v1 wire shape without accepting unknown fields."""

    mapping = _mapping(payload, "projection surface")
    _exact_fields(
        mapping,
        {
            "team_week_manifest_id",
            "league_id",
            "season",
            "week",
            "roster_id",
            "team_week_fingerprint",
            "projection_config_version",
            "scoring_policy_version",
            "source_fingerprints",
            "entries",
            "planning_lead_time_seconds",
            "finalization_policy_version",
            "cutoff_schedule_version",
            "schema_version",
            "fingerprint",
        },
    )
    sources = tuple(_decode_source(raw) for raw in _list(mapping, "source_fingerprints"))
    entries = tuple(_decode_entry(item) for item in _list(mapping, "entries"))
    surface = HistoricalProjectionSurface(
        team_week_manifest_id=_text(mapping, "team_week_manifest_id"),
        league_id=_text(mapping, "league_id"),
        season=_text(mapping, "season"),
        week=_integer(mapping, "week"),
        roster_id=_integer(mapping, "roster_id"),
        team_week_fingerprint=_text(mapping, "team_week_fingerprint"),
        projection_config_version=_text(mapping, "projection_config_version"),
        scoring_policy_version=_text(mapping, "scoring_policy_version"),
        source_fingerprints=sources,
        entries=entries,
        planning_lead_time_seconds=_number(mapping, "planning_lead_time_seconds"),
        finalization_policy_version=_text(mapping, "finalization_policy_version"),
        cutoff_schedule_version=_text(mapping, "cutoff_schedule_version"),
        schema_version=_text(mapping, "schema_version"),
    )
    return surface, _text(mapping, "fingerprint")


def _decode_source(payload: object) -> SourceFingerprint:
    """Decode one exact raw-source binding."""

    mapping = _mapping(payload, "source fingerprint")
    _exact_fields(mapping, {"name", "content_hash", "version"})
    return SourceFingerprint(
        _text(mapping, "name"),
        _text(mapping, "content_hash"),
        _text(mapping, "version"),
    )


def _decode_entry(payload: object) -> ProjectionSurfaceEntry:
    """Decode one mutually exclusive projection-or-failure record."""

    mapping = _mapping(payload, "projection entry")
    _exact_fields(
        mapping,
        {
            "decision_time",
            "player_id",
            "game_id",
            "projection",
            "failure_reason",
            "failure_detail",
        },
    )
    raw_projection = mapping["projection"]
    return ProjectionSurfaceEntry(
        decision_time=_aware_datetime(mapping, "decision_time"),
        player_id=_text(mapping, "player_id"),
        game_id=_text(mapping, "game_id"),
        projection=(None if raw_projection is None else decode_projection_snapshot(raw_projection)),
        failure_reason=_optional_text(mapping, "failure_reason"),
        failure_detail=_optional_text(mapping, "failure_detail"),
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    """Require one JSON object at the artifact boundary."""

    if not isinstance(value, dict):
        raise HistoricalProjectionSurfaceError(f"{label} must be an object")
    return value


def _list(mapping: dict[str, Any], field: str) -> list[object]:
    """Require one JSON array field."""

    value = mapping.get(field)
    if not isinstance(value, list):
        raise HistoricalProjectionSurfaceError(f"{field} must be an array")
    return value


def _exact_fields(mapping: dict[str, Any], fields: set[str]) -> None:
    """Reject missing or unknown artifact fields."""

    if set(mapping) != fields:
        raise HistoricalProjectionSurfaceError("Projection-surface fields are invalid")


def _text(mapping: dict[str, Any], field: str) -> str:
    """Require one non-empty string field."""

    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HistoricalProjectionSurfaceError(f"{field} must be a non-empty string")
    return value


def _optional_text(mapping: dict[str, Any], field: str) -> str | None:
    """Require a nullable string field to be non-empty when present."""

    value = mapping.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoricalProjectionSurfaceError(f"{field} must be null or non-empty")
    return value


def _integer(mapping: dict[str, Any], field: str) -> int:
    """Require one strict integer field."""

    value = mapping.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise HistoricalProjectionSurfaceError(f"{field} must be an integer")
    return value


def _number(mapping: dict[str, Any], field: str) -> float:
    """Require one numeric field; the domain model checks its range."""

    value = mapping.get(field)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise HistoricalProjectionSurfaceError(f"{field} must be numeric")
    return float(value)


def _aware_datetime(mapping: dict[str, Any], field: str) -> datetime:
    """Parse one timezone-aware ISO timestamp."""

    try:
        value = datetime.fromisoformat(_text(mapping, field))
    except ValueError as error:
        raise HistoricalProjectionSurfaceError(f"{field} must be an ISO timestamp") from error
    if value.tzinfo is None:
        raise HistoricalProjectionSurfaceError(f"{field} must be timezone-aware")
    return value


__all__ = (
    "load_historical_projection_surface_artifact",
    "write_historical_projection_surface_artifact",
)
