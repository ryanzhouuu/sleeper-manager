"""Round-trip and strictness coverage for the local feature-dataset cache codec."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sleeper_manager.backtesting.experiments.feature_dataset_cache import (
    FeatureDatasetCacheError,
    decode_dataset,
    encode_dataset,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    DatasetSourceVersion,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
    OpponentStatsFallback,
    PaceStatsFallback,
)

BASE = datetime(2025, 1, 1, 18, tzinfo=UTC)


def source(provider_id: str, hours: int = 3) -> SourceMetadata:
    """Build deterministic lineage metadata for cache fixtures."""
    return SourceMetadata(
        "fixture",
        provider_id,
        BASE + timedelta(hours=hours),
        BASE + timedelta(hours=hours - 1),
        "2",
        f"hash-{provider_id}",
    )


def row(game_id: str, **overrides: Any) -> HistoricalFeatureRow:
    """Build one cache fixture row with falsy-but-present defaults."""
    values: dict[str, Any] = {
        "dataset_version": "fixture-v1",
        "available_as_of": BASE - timedelta(minutes=30),
        "player_id": "p1",
        "sleeper_id": None,
        "game_id": game_id,
        "game_start": BASE,
        "team_id": "CHI",
        "opponent_team_id": "WAS",
        "opponent_abbreviation": "was",
        "is_home": False,
        "days_rest": 0,
        "is_back_to_back": False,
        "availability_status": AvailabilityStatus.AVAILABLE,
        "availability_observation": AvailabilityObservation.MISSING_REPORT,
        "availability_detail": None,
        "availability_observed_at": None,
        "prior_games": 0,
        "prior_minutes_mean": 0.0,
        "prior_minutes_last": None,
        "prior_start_rate": None,
        "target_minutes": 0.0,
        "target_started": False,
        "target_did_play": False,
        "target_box_score": BoxScoreLine(),
        "target_line_points": 0,
        "target_line_rebounds": 0,
        "target_line_assists": 0,
        "target_line_steals": 0,
        "target_line_blocks": 0,
        "target_line_turnovers": 0,
        "source_lineage": (),
        "opponent_offensive_rating": None,
        "opponent_defensive_rating": None,
        "league_defensive_rating": None,
        "opponent_pace": None,
        "opponent_sample_size": 0,
        "opponent_stats_fallback": OpponentStatsFallback.MISSING,
        "opponent_offense_band": "unknown",
        "opponent_defense_band": "unknown",
        "opponent_pace_band": "unknown",
        "own_team_pace": None,
        "own_team_pace_sample_size": 0,
        "own_team_pace_fallback": PaceStatsFallback.LEAGUE_AVERAGE,
        "expected_matchup_pace": None,
        "baseline_exposure_pace": None,
        "pace_factor": None,
        "prior_venue_id": "",
        "destination_venue_id": None,
        "travel_distance_miles": 0.0,
        "time_zone_change_hours": 0.0,
        "travel_direction": "unknown",
        "travel_fallback": "unknown_venue",
        "outcome_finalized_at": None,
    }
    values.update(overrides)
    return HistoricalFeatureRow(**values)  # type: ignore[arg-type]


def dataset(rows: tuple[HistoricalFeatureRow, ...]) -> HistoricalFeatureDataset:
    """Wrap fixture rows in a versioned dataset with source identities."""
    return HistoricalFeatureDataset(
        "fixture-v1",
        "5",
        BASE,
        (DatasetSourceVersion("fixture", "v2", ("game-1", "game-2")),),
        rows,
    )


def test_dataset_round_trip_preserves_rows_exactly() -> None:
    """Prove cached rows survive serialization without losing fidelity."""
    original = dataset(
        (
            row("g1"),
            row(
                "g2",
                sleeper_id="sleeper-1",
                is_home=True,
                days_rest=3,
                is_back_to_back=True,
                availability_status=AvailabilityStatus.QUESTIONABLE,
                availability_observation=AvailabilityObservation.REPORTED,
                availability_detail="ankle",
                availability_observed_at=BASE - timedelta(hours=2),
                prior_games=41,
                prior_minutes_mean=28.5,
                prior_minutes_last=33.0,
                prior_start_rate=0.9,
                target_minutes=27.5,
                target_started=True,
                target_did_play=True,
                target_box_score=BoxScoreLine(points=24, three_pointers_made=3, turnovers=2),
                target_line_points=24,
                target_line_assists=7,
                source_lineage=(source("game-2"), source("game-2-early", hours=1)),
                opponent_offensive_rating=112.5,
                opponent_defensive_rating=108.0,
                league_defensive_rating=110.2,
                opponent_pace=99.5,
                opponent_sample_size=10,
                opponent_stats_fallback=OpponentStatsFallback.OBSERVED,
                opponent_offense_band="high",
                opponent_defense_band="low",
                opponent_pace_band="medium",
                own_team_pace=101.0,
                own_team_pace_sample_size=12,
                own_team_pace_fallback=PaceStatsFallback.SHRUNK,
                expected_matchup_pace=100.25,
                baseline_exposure_pace=98.75,
                pace_factor=1.0125,
                prior_venue_id="venue-1",
                destination_venue_id="venue-2",
                travel_distance_miles=1500.5,
                time_zone_change_hours=-2.0,
                travel_direction="west",
                travel_fallback="observed",
                outcome_finalized_at=BASE + timedelta(hours=2),
            ),
            row(
                "g3",
                availability_status=AvailabilityStatus.OUT,
                availability_observation=AvailabilityObservation.TEAM_NOT_YET_SUBMITTED,
                opponent_stats_fallback=OpponentStatsFallback.SHRUNK,
                own_team_pace_fallback=PaceStatsFallback.PRIOR_SEASON,
                source_lineage=(source("game-3"),),
            ),
        )
    )

    payload = encode_dataset(original)
    json.dumps(payload)
    restored = decode_dataset(json.loads(json.dumps(payload)))

    assert restored == original
    assert [item.game_id for item in restored.rows] == ["g1", "g2", "g3"]


def test_decode_rejects_unknown_and_missing_fields() -> None:
    """Fail explicitly on schema drift instead of decoding a partial row."""
    payload = encode_dataset(dataset((row("g1"),)))
    row_payload = payload["rows"][0]
    assert isinstance(row_payload, dict)

    unknown = dict(row_payload)
    unknown["future_field"] = 1
    with pytest.raises(FeatureDatasetCacheError, match="unknown fields"):
        decode_dataset({**payload, "rows": [unknown]})

    missing = dict(row_payload)
    del missing["target_box_score"]
    with pytest.raises(FeatureDatasetCacheError, match="missing fields"):
        decode_dataset({**payload, "rows": [missing]})


def test_decode_rejects_mistyped_and_naive_values() -> None:
    """Require exact scalar shapes and timezone-aware timestamps."""
    payload = encode_dataset(dataset((row("g1"),)))

    def with_row(value: dict[str, Any]) -> dict[str, Any]:
        assert isinstance(payload["rows"][0], dict)
        return {**payload, "rows": [{**payload["rows"][0], **value}]}

    with pytest.raises(FeatureDatasetCacheError, match="availability_status"):
        decode_dataset(with_row({"availability_status": "injured"}))
    with pytest.raises(FeatureDatasetCacheError, match="available_as_of"):
        decode_dataset(with_row({"available_as_of": "2025-01-01T18:00:00"}))
    with pytest.raises(FeatureDatasetCacheError, match="target_started"):
        decode_dataset(with_row({"target_started": 1}))
    with pytest.raises(FeatureDatasetCacheError, match="prior_games"):
        decode_dataset(with_row({"prior_games": True}))


def test_decode_rejects_non_mapping_payload() -> None:
    """Refuse top-level corruption before any field is trusted."""
    with pytest.raises(FeatureDatasetCacheError, match="must be a mapping"):
        decode_dataset([])
