"""Strict local history input: declared coverage cannot populate absent source fields."""

import json
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    HybridHistory,
    JointGameObservation,
    ParticipationOpportunity,
    require_aware,
)


def _shape(value: object, record_type: type[Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {field.name for field in fields(record_type)}:
        raise HybridProjectionError("invalid_history_artifact_shape")
    return value


def _unique_fields(items: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in items}) != len(items):
        raise HybridProjectionError("duplicate_history_artifact_field")
    return dict(items)


def load_hybrid_history_artifact(path: Path) -> HybridHistory:
    """Read typed evidence; unknown fields, coercions, and claimed absent stats are errors."""
    try:
        encoded = path.read_bytes()
        raw = _shape(json.loads(encoded, object_pairs_hook=_unique_fields), HybridHistory)
        for item in raw.get("observations", []):
            row = _shape(item, JointGameObservation)
            line = _shape(row.get("line"), BoxScoreLine)
            if not set(row.get("verified_fields", ())).issubset(line):
                raise HybridProjectionError("declared_coverage_without_source_value")
        for item in raw.get("opportunities", []):
            row = _shape(item, ParticipationOpportunity)
            if row.get("availability") is not None:
                _shape(row["availability"], GameAvailability)
        return TypeAdapter(HybridHistory).validate_json(encoded, strict=True)
    except (OSError, ValueError, TypeError, ValidationError) as error:
        if isinstance(error, HybridProjectionError):
            raise
        raise HybridProjectionError("invalid_history_artifact") from error


@dataclass(frozen=True, slots=True)
class CutoffAvailability:
    cutoff: datetime
    player_id: str
    report: GameAvailability

    def __post_init__(self) -> None:
        require_aware(self.cutoff)
        if not self.player_id.strip():
            raise HybridProjectionError("missing_availability_player")


def load_hybrid_availability_artifact(
    path: Path,
) -> dict[tuple[datetime, str, str], GameAvailability]:
    """Load explicit cutoff/game assignments; duplicate assignments are ambiguous."""
    encoded = path.read_bytes()
    raw = json.loads(encoded, object_pairs_hook=_unique_fields)
    if not isinstance(raw, list):
        raise HybridProjectionError("invalid_availability_artifact_shape")
    for item in raw:
        row = _shape(item, CutoffAvailability)
        _shape(row.get("report"), GameAvailability)
    rows = TypeAdapter(tuple[CutoffAvailability, ...]).validate_json(encoded, strict=True)
    assignments = {(row.cutoff, row.player_id, row.report.game_id): row.report for row in rows}
    if len(assignments) != len(rows):
        raise HybridProjectionError("duplicate_availability_assignment")
    return assignments
