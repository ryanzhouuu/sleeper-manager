"""Versioned local cache for built historical feature datasets.

Setup (raw parsing, injury acquisition, feature construction) costs minutes while its
inputs change rarely. This module persists one built dataset per workspace under a key
derived from source file bytes, scoring policy, feature schema, and source revision, so
repeated evaluations restore it in seconds. Any missing, corrupt, stale, or incompatible
cache falls back to a full rebuild; a bad cache is never used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.artifacts import atomic_write_json, canonical_json_bytes
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    FEATURE_SCHEMA_VERSION,
    AvailabilityObservation,
    DatasetSourceVersion,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
    OpponentStatsFallback,
    PaceStatsFallback,
)

CACHE_FORMAT_VERSION = "historical-feature-dataset-cache-v1"

_CACHE_FILENAME = "historical-feature-dataset-cache.json"

_ROW_FIELDS: tuple[str, ...] = (
    "dataset_version",
    "available_as_of",
    "player_id",
    "sleeper_id",
    "game_id",
    "game_start",
    "team_id",
    "opponent_team_id",
    "opponent_abbreviation",
    "is_home",
    "days_rest",
    "is_back_to_back",
    "availability_status",
    "availability_observation",
    "availability_detail",
    "availability_observed_at",
    "prior_games",
    "prior_minutes_mean",
    "prior_minutes_last",
    "prior_start_rate",
    "target_minutes",
    "target_started",
    "target_did_play",
    "target_box_score",
    "target_line_points",
    "target_line_rebounds",
    "target_line_assists",
    "target_line_steals",
    "target_line_blocks",
    "target_line_turnovers",
    "source_lineage",
    "opponent_offensive_rating",
    "opponent_defensive_rating",
    "league_defensive_rating",
    "opponent_pace",
    "opponent_sample_size",
    "opponent_stats_fallback",
    "opponent_offense_band",
    "opponent_defense_band",
    "opponent_pace_band",
    "own_team_pace",
    "own_team_pace_sample_size",
    "own_team_pace_fallback",
    "expected_matchup_pace",
    "baseline_exposure_pace",
    "pace_factor",
    "prior_venue_id",
    "destination_venue_id",
    "travel_distance_miles",
    "time_zone_change_hours",
    "travel_direction",
    "travel_fallback",
    "outcome_finalized_at",
)

_BOX_SCORE_FIELDS: tuple[str, ...] = (
    "points",
    "rebounds",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "three_pointers_made",
    "technical_fouls",
    "flagrant_fouls",
)


class FeatureDatasetCacheError(ValueError):
    """Raised when cached dataset payload cannot be trusted or decoded."""


def encode_dataset(dataset: HistoricalFeatureDataset) -> dict[str, Any]:
    """Render one dataset to JSON-safe mappings preserving row order."""
    return {
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "generated_at": dataset.generated_at.isoformat(),
        "source_versions": [_encode_source_version(item) for item in dataset.source_versions],
        "rows": [_encode_row(item) for item in dataset.rows],
    }


def decode_dataset(payload: object) -> HistoricalFeatureDataset:
    """Rebuild one dataset, rejecting unknown, missing, or mistyped fields."""
    mapping = _as_mapping(payload, "dataset")
    _require_exact_keys(
        mapping,
        ("dataset_version", "feature_schema_version", "generated_at", "source_versions", "rows"),
        "dataset",
    )
    rows = mapping["rows"]
    if not isinstance(rows, list):
        raise FeatureDatasetCacheError("Cached dataset rows must be a list")
    versions = mapping["source_versions"]
    if not isinstance(versions, list):
        raise FeatureDatasetCacheError("Cached dataset source versions must be a list")
    feature_schema = _as_str(mapping["feature_schema_version"], "feature_schema_version")
    return HistoricalFeatureDataset(
        dataset_version=_as_str(mapping["dataset_version"], "dataset_version"),
        feature_schema_version=feature_schema,
        generated_at=_as_datetime(mapping["generated_at"], "generated_at"),
        source_versions=tuple(
            _decode_source_version(item, position) for position, item in enumerate(versions)
        ),
        rows=tuple(_decode_row(item, position) for position, item in enumerate(rows)),
    )


def _encode_row(row: HistoricalFeatureRow) -> dict[str, Any]:
    """Render one feature row with enums and timestamps in stable scalar forms."""
    return {
        "dataset_version": row.dataset_version,
        "available_as_of": row.available_as_of.isoformat(),
        "player_id": row.player_id,
        "sleeper_id": row.sleeper_id,
        "game_id": row.game_id,
        "game_start": row.game_start.isoformat(),
        "team_id": row.team_id,
        "opponent_team_id": row.opponent_team_id,
        "opponent_abbreviation": row.opponent_abbreviation,
        "is_home": row.is_home,
        "days_rest": row.days_rest,
        "is_back_to_back": row.is_back_to_back,
        "availability_status": row.availability_status.value,
        "availability_observation": row.availability_observation.value,
        "availability_detail": row.availability_detail,
        "availability_observed_at": _optional_isoformat(row.availability_observed_at),
        "prior_games": row.prior_games,
        "prior_minutes_mean": row.prior_minutes_mean,
        "prior_minutes_last": row.prior_minutes_last,
        "prior_start_rate": row.prior_start_rate,
        "target_minutes": row.target_minutes,
        "target_started": row.target_started,
        "target_did_play": row.target_did_play,
        "target_box_score": _encode_box_score(row.target_box_score),
        "target_line_points": row.target_line_points,
        "target_line_rebounds": row.target_line_rebounds,
        "target_line_assists": row.target_line_assists,
        "target_line_steals": row.target_line_steals,
        "target_line_blocks": row.target_line_blocks,
        "target_line_turnovers": row.target_line_turnovers,
        "source_lineage": [_encode_source(item) for item in row.source_lineage],
        "opponent_offensive_rating": row.opponent_offensive_rating,
        "opponent_defensive_rating": row.opponent_defensive_rating,
        "league_defensive_rating": row.league_defensive_rating,
        "opponent_pace": row.opponent_pace,
        "opponent_sample_size": row.opponent_sample_size,
        "opponent_stats_fallback": row.opponent_stats_fallback.value,
        "opponent_offense_band": row.opponent_offense_band,
        "opponent_defense_band": row.opponent_defense_band,
        "opponent_pace_band": row.opponent_pace_band,
        "own_team_pace": row.own_team_pace,
        "own_team_pace_sample_size": row.own_team_pace_sample_size,
        "own_team_pace_fallback": row.own_team_pace_fallback.value,
        "expected_matchup_pace": row.expected_matchup_pace,
        "baseline_exposure_pace": row.baseline_exposure_pace,
        "pace_factor": row.pace_factor,
        "prior_venue_id": row.prior_venue_id,
        "destination_venue_id": row.destination_venue_id,
        "travel_distance_miles": row.travel_distance_miles,
        "time_zone_change_hours": row.time_zone_change_hours,
        "travel_direction": row.travel_direction,
        "travel_fallback": row.travel_fallback,
        "outcome_finalized_at": _optional_isoformat(row.outcome_finalized_at),
    }


def _decode_row(payload: object, position: int) -> HistoricalFeatureRow:
    """Rebuild one feature row, rejecting any schema drift in its fields."""
    label = f"row {position}"
    mapping = _as_mapping(payload, label)
    _require_exact_keys(mapping, _ROW_FIELDS, label)
    box_score = mapping["target_box_score"]
    if not isinstance(box_score, dict):
        raise FeatureDatasetCacheError(f"Cached {label} box score must be a mapping")
    _require_exact_keys(box_score, _BOX_SCORE_FIELDS, f"{label} box score")
    lineage = mapping["source_lineage"]
    if not isinstance(lineage, list):
        raise FeatureDatasetCacheError(f"Cached {label} lineage must be a list")
    return HistoricalFeatureRow(
        dataset_version=_as_str(mapping["dataset_version"], f"{label} dataset_version"),
        available_as_of=_as_datetime(mapping["available_as_of"], f"{label} available_as_of"),
        player_id=_as_str(mapping["player_id"], f"{label} player_id"),
        sleeper_id=_as_optional_str(mapping["sleeper_id"], f"{label} sleeper_id"),
        game_id=_as_str(mapping["game_id"], f"{label} game_id"),
        game_start=_as_datetime(mapping["game_start"], f"{label} game_start"),
        team_id=_as_str(mapping["team_id"], f"{label} team_id"),
        opponent_team_id=_as_str(mapping["opponent_team_id"], f"{label} opponent_team_id"),
        opponent_abbreviation=_as_str(
            mapping["opponent_abbreviation"], f"{label} opponent_abbreviation"
        ),
        is_home=_as_bool(mapping["is_home"], f"{label} is_home"),
        days_rest=_as_optional_int(mapping["days_rest"], f"{label} days_rest"),
        is_back_to_back=_as_optional_bool(mapping["is_back_to_back"], f"{label} is_back_to_back"),
        availability_status=_as_enum(
            AvailabilityStatus, mapping["availability_status"], f"{label} availability_status"
        ),
        availability_observation=_as_enum(
            AvailabilityObservation,
            mapping["availability_observation"],
            f"{label} availability_observation",
        ),
        availability_detail=_as_optional_str(
            mapping["availability_detail"], f"{label} availability_detail"
        ),
        availability_observed_at=_as_optional_datetime(
            mapping["availability_observed_at"], f"{label} availability_observed_at"
        ),
        prior_games=_as_int(mapping["prior_games"], f"{label} prior_games"),
        prior_minutes_mean=_as_optional_float(
            mapping["prior_minutes_mean"], f"{label} prior_minutes_mean"
        ),
        prior_minutes_last=_as_optional_float(
            mapping["prior_minutes_last"], f"{label} prior_minutes_last"
        ),
        prior_start_rate=_as_optional_float(
            mapping["prior_start_rate"], f"{label} prior_start_rate"
        ),
        target_minutes=_as_optional_float(mapping["target_minutes"], f"{label} target_minutes"),
        target_started=_as_bool(mapping["target_started"], f"{label} target_started"),
        target_did_play=_as_bool(mapping["target_did_play"], f"{label} target_did_play"),
        target_box_score=_decode_box_score(box_score, label),
        target_line_points=_as_int(mapping["target_line_points"], f"{label} target_line_points"),
        target_line_rebounds=_as_int(
            mapping["target_line_rebounds"], f"{label} target_line_rebounds"
        ),
        target_line_assists=_as_int(mapping["target_line_assists"], f"{label} target_line_assists"),
        target_line_steals=_as_int(mapping["target_line_steals"], f"{label} target_line_steals"),
        target_line_blocks=_as_int(mapping["target_line_blocks"], f"{label} target_line_blocks"),
        target_line_turnovers=_as_int(
            mapping["target_line_turnovers"], f"{label} target_line_turnovers"
        ),
        source_lineage=tuple(
            _decode_source(item, f"{label} lineage {index}") for index, item in enumerate(lineage)
        ),
        opponent_offensive_rating=_as_optional_float(
            mapping["opponent_offensive_rating"], f"{label} opponent_offensive_rating"
        ),
        opponent_defensive_rating=_as_optional_float(
            mapping["opponent_defensive_rating"], f"{label} opponent_defensive_rating"
        ),
        league_defensive_rating=_as_optional_float(
            mapping["league_defensive_rating"], f"{label} league_defensive_rating"
        ),
        opponent_pace=_as_optional_float(mapping["opponent_pace"], f"{label} opponent_pace"),
        opponent_sample_size=_as_int(
            mapping["opponent_sample_size"], f"{label} opponent_sample_size"
        ),
        opponent_stats_fallback=_as_enum(
            OpponentStatsFallback, mapping["opponent_stats_fallback"], f"{label} opponent_fallback"
        ),
        opponent_offense_band=_as_str(
            mapping["opponent_offense_band"], f"{label} opponent_offense_band"
        ),
        opponent_defense_band=_as_str(
            mapping["opponent_defense_band"], f"{label} opponent_defense_band"
        ),
        opponent_pace_band=_as_str(mapping["opponent_pace_band"], f"{label} opponent_pace_band"),
        own_team_pace=_as_optional_float(mapping["own_team_pace"], f"{label} own_team_pace"),
        own_team_pace_sample_size=_as_int(
            mapping["own_team_pace_sample_size"], f"{label} own_team_pace_sample_size"
        ),
        own_team_pace_fallback=_as_enum(
            PaceStatsFallback, mapping["own_team_pace_fallback"], f"{label} own_team_pace_fallback"
        ),
        expected_matchup_pace=_as_optional_float(
            mapping["expected_matchup_pace"], f"{label} expected_matchup_pace"
        ),
        baseline_exposure_pace=_as_optional_float(
            mapping["baseline_exposure_pace"], f"{label} baseline_exposure_pace"
        ),
        pace_factor=_as_optional_float(mapping["pace_factor"], f"{label} pace_factor"),
        prior_venue_id=_as_optional_str(mapping["prior_venue_id"], f"{label} prior_venue_id"),
        destination_venue_id=_as_optional_str(
            mapping["destination_venue_id"], f"{label} destination_venue_id"
        ),
        travel_distance_miles=_as_optional_float(
            mapping["travel_distance_miles"], f"{label} travel_distance_miles"
        ),
        time_zone_change_hours=_as_optional_float(
            mapping["time_zone_change_hours"], f"{label} time_zone_change_hours"
        ),
        travel_direction=_as_str(mapping["travel_direction"], f"{label} travel_direction"),
        travel_fallback=_as_str(mapping["travel_fallback"], f"{label} travel_fallback"),
        outcome_finalized_at=_as_optional_datetime(
            mapping["outcome_finalized_at"], f"{label} outcome_finalized_at"
        ),
    )


def _encode_box_score(line: BoxScoreLine) -> dict[str, int]:
    """Render one box-score line in field order."""
    return {name: getattr(line, name) for name in _BOX_SCORE_FIELDS}


def _decode_box_score(payload: dict[str, Any], label: str) -> BoxScoreLine:
    """Rebuild one box-score line, rejecting any schema drift in its fields."""
    _require_exact_keys(payload, _BOX_SCORE_FIELDS, f"{label} box score")
    values = {
        name: _as_int(payload[name], f"{label} box score {name}") for name in _BOX_SCORE_FIELDS
    }
    return BoxScoreLine(**values)


def _encode_source(item: SourceMetadata) -> dict[str, Any]:
    """Render lineage metadata with timestamps in interchange form."""
    return {
        "provider": item.provider,
        "provider_id": item.provider_id,
        "retrieved_at": item.retrieved_at.isoformat(),
        "source_updated_at": _optional_isoformat(item.source_updated_at),
        "schema_version": item.schema_version,
        "content_hash": item.content_hash,
    }


def _decode_source(payload: object, label: str) -> SourceMetadata:
    """Rebuild lineage metadata, rejecting any schema drift in its fields."""
    mapping = _as_mapping(payload, label)
    _require_exact_keys(
        mapping,
        (
            "provider",
            "provider_id",
            "retrieved_at",
            "source_updated_at",
            "schema_version",
            "content_hash",
        ),
        label,
    )
    return SourceMetadata(
        provider=_as_str(mapping["provider"], f"{label} provider"),
        provider_id=_as_str(mapping["provider_id"], f"{label} provider_id"),
        retrieved_at=_as_datetime(mapping["retrieved_at"], f"{label} retrieved_at"),
        source_updated_at=_as_optional_datetime(
            mapping["source_updated_at"], f"{label} source_updated_at"
        ),
        schema_version=_as_str(mapping["schema_version"], f"{label} schema_version"),
        content_hash=_as_optional_str(mapping["content_hash"], f"{label} content_hash"),
    )


def _encode_source_version(item: DatasetSourceVersion) -> dict[str, Any]:
    """Render one dataset-level source identity entry."""
    return {
        "provider": item.provider,
        "schema_version": item.schema_version,
        "source_ids": list(item.source_ids),
    }


def _decode_source_version(payload: object, position: int) -> DatasetSourceVersion:
    """Rebuild one dataset-level source identity entry."""
    label = f"source version {position}"
    mapping = _as_mapping(payload, label)
    _require_exact_keys(mapping, ("provider", "schema_version", "source_ids"), label)
    identifiers = mapping["source_ids"]
    if not isinstance(identifiers, list) or not all(isinstance(item, str) for item in identifiers):
        raise FeatureDatasetCacheError(f"Cached {label} source ids must be strings")
    return DatasetSourceVersion(
        provider=_as_str(mapping["provider"], f"{label} provider"),
        schema_version=_as_str(mapping["schema_version"], f"{label} schema_version"),
        source_ids=tuple(identifiers),
    )


def _optional_isoformat(value: datetime | None) -> str | None:
    """Render an optional timestamp without admitting naive values."""
    return value.isoformat() if value is not None else None


def _as_mapping(payload: object, label: str) -> dict[str, Any]:
    """Require a JSON mapping for one cached structure."""
    if not isinstance(payload, dict):
        raise FeatureDatasetCacheError(f"Cached {label} must be a mapping")
    return payload


def _require_exact_keys(mapping: dict[str, Any], names: tuple[str, ...], label: str) -> None:
    """Reject missing or unknown fields so schema drift fails explicitly."""
    missing = [name for name in names if name not in mapping]
    if missing:
        raise FeatureDatasetCacheError(f"Cached {label} is missing fields: {sorted(missing)}")
    unknown = [key for key in mapping if key not in names]
    if unknown:
        raise FeatureDatasetCacheError(f"Cached {label} has unknown fields: {sorted(unknown)}")


def _as_str(value: object, label: str) -> str:
    """Require a string for one cached field."""
    if not isinstance(value, str):
        raise FeatureDatasetCacheError(f"Cached {label} must be a string")
    return value


def _as_optional_str(value: object, label: str) -> str | None:
    """Require a string or null for one cached field."""
    if value is None:
        return None
    return _as_str(value, label)


def _as_bool(value: object, label: str) -> bool:
    """Require a boolean for one cached field."""
    if not isinstance(value, bool):
        raise FeatureDatasetCacheError(f"Cached {label} must be a boolean")
    return value


def _as_optional_bool(value: object, label: str) -> bool | None:
    """Require a boolean or null for one cached field."""
    if value is None:
        return None
    return _as_bool(value, label)


def _as_int(value: object, label: str) -> int:
    """Require an integer for one cached field."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise FeatureDatasetCacheError(f"Cached {label} must be an integer")
    return value


def _as_optional_int(value: object, label: str) -> int | None:
    """Require an integer or null for one cached field."""
    if value is None:
        return None
    return _as_int(value, label)


def _as_optional_float(value: object, label: str) -> float | None:
    """Require a number or null for one cached field."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeatureDatasetCacheError(f"Cached {label} must be a number")
    return float(value)


def _as_datetime(value: object, label: str) -> datetime:
    """Require a timezone-aware timestamp for one cached field."""
    if not isinstance(value, str):
        raise FeatureDatasetCacheError(f"Cached {label} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FeatureDatasetCacheError(f"Cached {label} has an invalid timestamp") from error
    if parsed.tzinfo is None:
        raise FeatureDatasetCacheError(f"Cached {label} must be timezone-aware")
    return parsed


def _as_optional_datetime(value: object, label: str) -> datetime | None:
    """Require a timezone-aware timestamp or null for one cached field."""
    if value is None:
        return None
    return _as_datetime(value, label)


def _as_enum[EnumT: Enum](kind: type[EnumT], value: object, label: str) -> EnumT:
    """Require a known enum value for one cached field."""
    try:
        return kind(value)
    except ValueError as error:
        raise FeatureDatasetCacheError(f"Cached {label} has an unknown value") from error


@dataclass(frozen=True, slots=True)
class DatasetCacheKey:
    """Content identities that must match before a cached dataset is reused."""

    raw_digest: str
    raw_files: tuple[str, ...]
    injuries_digest: str
    injury_files: tuple[str, ...]
    scoring_policy_version: str
    feature_schema_version: str
    source_revision: str


def dataset_cache_path(workspace: Path) -> Path:
    """Return the stable workspace location of the built dataset cache."""
    return workspace / _CACHE_FILENAME


def compute_cache_key(
    raw_dir: Path,
    injuries_dir: Path,
    *,
    scoring_policy: ScoringPolicy,
    source_revision: str,
) -> DatasetCacheKey:
    """Hash workspace source bytes without parsing them; missing inputs fail the lookup."""
    if not source_revision.strip():
        raise FeatureDatasetCacheError("Dataset cache source revision must not be empty")
    raw_digest, raw_files = _hash_tree(raw_dir, "raw sources")
    injuries_digest, injury_files = _hash_tree(injuries_dir, "injury archive")
    return DatasetCacheKey(
        raw_digest=raw_digest,
        raw_files=raw_files,
        injuries_digest=injuries_digest,
        injury_files=injury_files,
        scoring_policy_version=scoring_policy.version,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        source_revision=source_revision,
    )


def write_dataset_cache(
    path: Path, key: DatasetCacheKey, dataset: HistoricalFeatureDataset
) -> None:
    """Atomically persist one dataset with its key under a tamper-evident digest."""
    body = {
        "format_version": CACHE_FORMAT_VERSION,
        "key": {
            "raw_digest": key.raw_digest,
            "raw_files": list(key.raw_files),
            "injuries_digest": key.injuries_digest,
            "injury_files": list(key.injury_files),
            "scoring_policy_version": key.scoring_policy_version,
            "feature_schema_version": key.feature_schema_version,
            "source_revision": key.source_revision,
        },
        "dataset": encode_dataset(dataset),
    }
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    try:
        atomic_write_json(path, {**body, "content_hash": digest})
    except OSError as error:
        raise FeatureDatasetCacheError(f"Could not write dataset cache at {path}") from error


def read_dataset_cache(path: Path, expected: DatasetCacheKey) -> HistoricalFeatureDataset:
    """Restore one dataset, rejecting any integrity, format, or freshness mismatch."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} is unreadable") from error
    if not isinstance(payload, dict):
        raise FeatureDatasetCacheError(f"Dataset cache at {path} must be a mapping")
    if payload.get("format_version") != CACHE_FORMAT_VERSION:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} has an unsupported format")
    stored_hash = payload.get("content_hash")
    if not isinstance(stored_hash, str):
        raise FeatureDatasetCacheError(f"Dataset cache at {path} has no content hash")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if stored_hash != digest:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} failed integrity verification")
    key = _decode_key(body.get("key"), path)
    if key != expected:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} is stale")
    if "dataset" not in body:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} has no dataset")
    return decode_dataset(body["dataset"])


def _decode_key(payload: object, path: Path) -> DatasetCacheKey:
    """Rebuild a cache key, rejecting any schema drift in its fields."""
    if not isinstance(payload, dict):
        raise FeatureDatasetCacheError(f"Dataset cache at {path} has no key")
    names = (
        "raw_digest",
        "raw_files",
        "injuries_digest",
        "injury_files",
        "scoring_policy_version",
        "feature_schema_version",
        "source_revision",
    )
    missing = [name for name in names if name not in payload]
    if missing:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} key is missing {sorted(missing)}")
    unknown = [key for key in payload if key not in names]
    if unknown:
        raise FeatureDatasetCacheError(f"Dataset cache at {path} key has unknown {sorted(unknown)}")
    raw_files = payload["raw_files"]
    injury_files = payload["injury_files"]
    if not isinstance(raw_files, list) or not all(isinstance(item, str) for item in raw_files):
        raise FeatureDatasetCacheError(f"Dataset cache at {path} key has invalid raw files")
    if not isinstance(injury_files, list) or not all(
        isinstance(item, str) for item in injury_files
    ):
        raise FeatureDatasetCacheError(f"Dataset cache at {path} key has invalid injury files")
    for name in (
        "raw_digest",
        "injuries_digest",
        "scoring_policy_version",
        "feature_schema_version",
        "source_revision",
    ):
        if not isinstance(payload[name], str):
            raise FeatureDatasetCacheError(f"Dataset cache at {path} key has invalid {name}")
    return DatasetCacheKey(
        raw_digest=payload["raw_digest"],
        raw_files=tuple(raw_files),
        injuries_digest=payload["injuries_digest"],
        injury_files=tuple(injury_files),
        scoring_policy_version=payload["scoring_policy_version"],
        feature_schema_version=payload["feature_schema_version"],
        source_revision=payload["source_revision"],
    )


def _hash_tree(root: Path, label: str) -> tuple[str, tuple[str, ...]]:
    """Hash every file under one directory in stable order with names and sizes."""
    if not root.is_dir():
        raise FeatureDatasetCacheError(f"Dataset cache {label} directory is missing: {root}")
    digest = hashlib.sha256()
    names: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        names.append(relative)
        digest.update(relative.encode())
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise FeatureDatasetCacheError(f"Dataset cache cannot hash {path}") from error
    return digest.hexdigest(), tuple(names)


__all__ = (
    "CACHE_FORMAT_VERSION",
    "DatasetCacheKey",
    "FeatureDatasetCacheError",
    "compute_cache_key",
    "dataset_cache_path",
    "decode_dataset",
    "encode_dataset",
    "read_dataset_cache",
    "write_dataset_cache",
)
