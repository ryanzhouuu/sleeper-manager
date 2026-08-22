from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine

FEATURE_SCHEMA_VERSION = "5"


class HistoricalFeatureDatasetError(ValueError):
    pass


class AvailabilityObservation(StrEnum):
    REPORTED = "reported"
    NOT_LISTED = "not_listed"
    TEAM_NOT_YET_SUBMITTED = "team_not_yet_submitted"
    MISSING_REPORT = "missing_report"


class OpponentStatsFallback(StrEnum):
    OBSERVED = "observed"
    SHRUNK = "shrunk"
    LEAGUE_AVERAGE = "league_average"
    MISSING = "missing"


class PaceStatsFallback(StrEnum):
    OBSERVED = "observed"
    SHRUNK = "shrunk"
    PRIOR_SEASON = "prior_season"
    LEAGUE_AVERAGE = "league_average"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class HistoricalFeatureRow:
    dataset_version: str
    available_as_of: datetime
    player_id: str
    sleeper_id: str | None
    game_id: str
    game_start: datetime
    team_id: str
    opponent_team_id: str
    opponent_abbreviation: str
    is_home: bool
    days_rest: int | None
    is_back_to_back: bool | None
    availability_status: AvailabilityStatus
    availability_observation: AvailabilityObservation
    availability_detail: str | None
    availability_observed_at: datetime | None
    prior_games: int
    prior_minutes_mean: float | None
    prior_minutes_last: float | None
    prior_start_rate: float | None
    target_minutes: float | None
    target_started: bool
    target_did_play: bool
    target_box_score: BoxScoreLine
    target_line_points: int
    target_line_rebounds: int
    target_line_assists: int
    target_line_steals: int
    target_line_blocks: int
    target_line_turnovers: int
    source_lineage: tuple[SourceMetadata, ...]
    opponent_offensive_rating: float | None = None
    opponent_defensive_rating: float | None = None
    league_defensive_rating: float | None = None
    opponent_pace: float | None = None
    opponent_sample_size: int = 0
    opponent_stats_fallback: OpponentStatsFallback = OpponentStatsFallback.MISSING
    opponent_offense_band: str = "unknown"
    opponent_defense_band: str = "unknown"
    opponent_pace_band: str = "unknown"
    own_team_pace: float | None = None
    own_team_pace_sample_size: int = 0
    own_team_pace_fallback: PaceStatsFallback = PaceStatsFallback.MISSING
    expected_matchup_pace: float | None = None
    baseline_exposure_pace: float | None = None
    pace_factor: float | None = None
    prior_venue_id: str | None = None
    destination_venue_id: str | None = None
    travel_distance_miles: float | None = None
    time_zone_change_hours: float | None = None
    travel_direction: str = "unknown"
    travel_fallback: str = "unknown_venue"
    outcome_finalized_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DatasetSourceVersion:
    provider: str
    schema_version: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoricalFeatureDataset:
    dataset_version: str
    feature_schema_version: str
    generated_at: datetime
    source_versions: tuple[DatasetSourceVersion, ...]
    rows: Sequence[HistoricalFeatureRow]
