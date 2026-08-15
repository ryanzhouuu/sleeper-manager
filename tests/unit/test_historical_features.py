from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    BoxScoreLine,
    GameStatus,
    PlayerBoxScore,
    ScheduledGame,
    SourceMetadata,
    Team,
    TeamBoxScore,
)
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDatasetError,
    OpponentStatsFallback,
    build_historical_feature_dataset,
)
from sleeper_manager.integrations.nba.identity import (
    MappingConfidence,
    MappingMethod,
    PlayerMapping,
)
from sleeper_manager.integrations.nba.official_injury_mapping import HistoricalPlayerAvailability
from sleeper_manager.integrations.nba.official_injury_report import (
    parse_official_injury_report_text,
)

NOW = datetime(2026, 8, 9, 4, tzinfo=UTC)
SOURCE = SourceMetadata("fixture", "fixture", NOW)


def team(team_id: str, abbreviation: str) -> Team:
    return Team(team_id, abbreviation, abbreviation, None, SOURCE)


def game(game_id: str, start: str, status: GameStatus = GameStatus.FINAL) -> ScheduledGame:
    return ScheduledGame(
        game_id,
        datetime.fromisoformat(start).replace(tzinfo=UTC),
        status,
        "CHI",
        "WAS",
        None,
        SOURCE,
    )


def box(
    game_id: str,
    player_id: str,
    start: str,
    minutes: float,
    started: bool,
    team_id: str = "CHI",
) -> PlayerBoxScore:
    return PlayerBoxScore(
        game_id,
        player_id,
        team_id,
        datetime.fromisoformat(start).replace(tzinfo=UTC),
        started,
        True,
        minutes,
        BoxScoreLine(
            points=10,
            three_pointers_made=3,
            technical_fouls=1,
            flagrant_fouls=1,
        ),
        SOURCE,
    )


def test_historical_feature_dataset_uses_only_prior_player_history_and_schedule_context() -> None:
    target = game("game-2", "2025-01-02T01:00:00")
    previous = game("game-1", "2025-01-01T01:00:00")
    dataset = build_historical_feature_dataset(
        box_scores=[
            box("game-1", "espn-1", "2025-01-01T01:00:00", 30, True),
            box("game-2", "espn-1", "2025-01-02T01:00:00", 22, False),
        ],
        games=[previous, target],
        teams=[team("CHI", "CHI"), team("WAS", "WAS")],
        player_mappings=[
            PlayerMapping(
                "sleeper-1",
                "espn-1",
                MappingMethod.STABLE_ID,
                MappingConfidence.HIGH,
                "fixture",
            )
        ],
        injury_reports=[],
        availability=[],
        decision_cutoffs={
            "game-1": datetime(2025, 1, 1, tzinfo=UTC),
            "game-2": datetime(2025, 1, 2, tzinfo=UTC),
        },
        dataset_version="nba-features-2025-v1",
        generated_at=NOW,
    )

    row = dataset.rows[-1]
    assert row.sleeper_id == "sleeper-1"
    assert row.opponent_abbreviation == "was"
    assert row.is_home
    assert row.days_rest == 0
    assert row.is_back_to_back
    assert row.prior_games == 1
    assert row.prior_minutes_mean == 30
    assert row.prior_start_rate == 1
    assert row.target_minutes == 22
    assert row.target_box_score.three_pointers_made == 3
    assert row.target_box_score.technical_fouls == 1
    assert row.target_box_score.flagrant_fouls == 1
    assert row.dataset_version == "nba-features-2025-v1"
    assert dataset.source_versions[0].provider == "fixture"


def test_historical_feature_dataset_uses_latest_report_at_cutoff_and_distinguishes_missing() -> (
    None
):
    target = game("game-2", "2025-01-02T23:00:00")
    report_source = SourceMetadata(
        "nba_official_injury_report",
        "report",
        NOW,
        datetime(2025, 1, 2, 0, 30, tzinfo=UTC),
    )
    report_text = """
    Injury Report: 01/01/25 07:30 PM
    Game Date Game Time Matchup Team Player Name Current Status Reason
    01/02/2025 07:00 (ET) CHI@WAS Chicago Bulls Craig, Torrey Out Injury/Illness - Rest
    Washington Wizards NOT YET SUBMITTED
    """
    report = parse_official_injury_report_text(report_text, source=report_source)
    unrelated_report_text = """
    Injury Report: 01/01/25 07:30 PM
    Game Date Game Time Matchup Team Player Name Current Status Reason
    01/02/2025 08:00 (ET) CHI@BOS Chicago Bulls NOT YET SUBMITTED
    Boston Celtics NOT YET SUBMITTED
    """
    unrelated_report = parse_official_injury_report_text(
        unrelated_report_text,
        source=report_source,
    )
    availability = HistoricalPlayerAvailability(
        "espn-1",
        report.entries[0].game_date,
        report.entries[0].game_time,
        report.entries[0].matchup,
        report.entries[0].team_abbreviation,
        AvailabilityStatus.OUT,
        report.entries[0].reason,
        report.published_at,
        report.source,
    )
    dataset = build_historical_feature_dataset(
        box_scores=[
            box("game-2", "espn-1", "2025-01-02T23:00:00", 0, False),
            box("game-2", "espn-was", "2025-01-02T23:00:00", 0, False, team_id="WAS"),
        ],
        games=[target],
        teams=[team("CHI", "CHI"), team("WAS", "WAS")],
        player_mappings=[],
        injury_reports=[report, unrelated_report],
        availability=[availability],
        decision_cutoffs={"game-2": datetime(2025, 1, 2, 23, tzinfo=UTC)},
        dataset_version="v1",
        generated_at=NOW,
    )

    row = dataset.rows[0]
    assert row.availability_status is AvailabilityStatus.OUT
    assert row.availability_observation is AvailabilityObservation.REPORTED
    assert row.availability_observed_at == report.published_at
    not_submitted = next(row for row in dataset.rows if row.player_id == "espn-was")
    assert not_submitted.availability_observation is AvailabilityObservation.TEAM_NOT_YET_SUBMITTED

    missing = build_historical_feature_dataset(
        box_scores=[box("game-2", "espn-2", "2025-01-02T23:00:00", 0, False)],
        games=[target],
        teams=[team("CHI", "CHI"), team("WAS", "WAS")],
        player_mappings=[],
        injury_reports=[],
        availability=[],
        decision_cutoffs={"game-2": datetime(2025, 1, 2, 23, tzinfo=UTC)},
        dataset_version="v1",
        generated_at=NOW,
    )
    assert missing.rows[0].availability_observation is AvailabilityObservation.MISSING_REPORT


def test_historical_feature_dataset_rejects_future_decision_cutoff() -> None:
    with pytest.raises(HistoricalFeatureDatasetError, match="after game start"):
        build_historical_feature_dataset(
            box_scores=[box("game-1", "espn-1", "2025-01-01T01:00:00", 30, True)],
            games=[game("game-1", "2025-01-01T01:00:00")],
            teams=[team("CHI", "CHI"), team("WAS", "WAS")],
            player_mappings=[],
            injury_reports=[],
            availability=[],
            decision_cutoffs={"game-1": datetime(2025, 1, 1, 2, tzinfo=UTC)},
            dataset_version="v1",
            generated_at=NOW,
        )


def test_historical_features_use_prior_only_opponent_stats_and_travel() -> None:
    previous = replace(
        game("game-1", "2025-01-01T01:00:00"),
        venue_id="1847",
        venue_name="United Center",
        venue_city="Chicago",
        venue_state="IL",
    )
    target = ScheduledGame(
        "game-2",
        datetime(2025, 1, 3, 1, tzinfo=UTC),
        GameStatus.FINAL,
        "WAS",
        "CHI",
        None,
        SOURCE,
        venue_id="1823",
        venue_name="Capital One Arena",
        venue_city="Washington",
        venue_state="DC",
    )
    prior_opponent = TeamBoxScore(
        "game-1",
        "WAS",
        "CHI",
        previous.start_time,
        110,
        100,
        90,
        20,
        10,
        12,
        SOURCE,
    )
    prior_league = TeamBoxScore(
        "league-game",
        "OTHER",
        "OTHER-2",
        previous.start_time,
        90,
        95,
        90,
        10,
        10,
        10,
        SOURCE,
    )
    future_opponent = replace(
        prior_opponent,
        game_id="future",
        played_at=datetime(2025, 1, 4, tzinfo=UTC),
        points=300,
    )
    inputs = dict(
        box_scores=[
            box("game-1", "espn-1", "2025-01-01T01:00:00", 30, True),
            box("game-2", "espn-1", "2025-01-03T01:00:00", 30, True),
        ],
        games=[previous, target],
        teams=[team("CHI", "CHI"), team("WAS", "WAS")],
        player_mappings=[],
        injury_reports=[],
        availability=[],
        decision_cutoffs={
            "game-1": datetime(2025, 1, 1, tzinfo=UTC),
            "game-2": datetime(2025, 1, 3, tzinfo=UTC),
        },
        dataset_version="v2",
        generated_at=NOW,
    )

    dataset = build_historical_feature_dataset(
        **inputs,
        team_box_scores=[prior_opponent, prior_league, future_opponent],
    )
    without_future = build_historical_feature_dataset(
        **inputs,
        team_box_scores=[prior_opponent, prior_league],
    )

    row = dataset.rows[-1]
    comparison = without_future.rows[-1]
    assert dataset.feature_schema_version == "2"
    assert row.opponent_sample_size == 1
    assert row.opponent_stats_fallback is OpponentStatsFallback.SHRUNK
    assert row.opponent_offensive_rating == comparison.opponent_offensive_rating
    assert row.opponent_defensive_rating == comparison.opponent_defensive_rating
    assert row.opponent_pace == comparison.opponent_pace
    assert row.opponent_offense_band in {"low", "medium", "high"}
    assert row.opponent_defense_band in {"low", "medium", "high"}
    assert row.opponent_pace_band in {"low", "medium", "high"}
    assert row.prior_venue_id == "1847"
    assert row.destination_venue_id == "1823"
    assert row.travel_distance_miles is not None and row.travel_distance_miles > 500
    assert row.time_zone_change_hours == 1
    assert row.travel_direction == "east"
    assert row.travel_fallback == "observed"


def test_player_history_resets_at_season_boundary() -> None:
    previous = game("previous", "2025-04-01T01:00:00")
    target = game("target", "2025-10-20T01:00:00")
    dataset = build_historical_feature_dataset(
        box_scores=[
            box("previous", "espn-1", "2025-04-01T01:00:00", 30, True),
            box("target", "espn-1", "2025-10-20T01:00:00", 30, True),
        ],
        games=[previous, target],
        teams=[team("CHI", "CHI"), team("WAS", "WAS")],
        player_mappings=[],
        injury_reports=[],
        availability=[],
        decision_cutoffs={
            "previous": datetime(2025, 4, 1, tzinfo=UTC),
            "target": datetime(2025, 10, 20, tzinfo=UTC),
        },
        dataset_version="v2",
        generated_at=NOW,
    )

    assert dataset.rows[-1].prior_games == 0
    assert dataset.rows[-1].prior_minutes_mean is None
