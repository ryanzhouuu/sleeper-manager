from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from sleeper_manager.domain.nba import (
    PlayerBoxScore,
    ScheduledGame,
    SourceMetadata,
    Team,
    TeamBoxScore,
)
from sleeper_manager.integrations.nba.historical_feature_calculations import (
    _availability_at_cutoff,
    _baseline_exposure_pace,
    _decision_cutoff,
    _expected_matchup_pace,
    _index_availability,
    _index_games,
    _index_player_mappings,
    _index_prior_box_scores,
    _index_report_matchups,
    _index_reports,
    _index_team_schedule,
    _index_teams,
    _last_minutes,
    _lineage,
    _mean_minutes,
    _opponent,
    _opponent_stats,
    _rest_features,
    _season_key,
    _source_versions,
    _start_rate,
    _team_pace,
    _validate_cutoff,
    _validate_timestamp,
)
from sleeper_manager.integrations.nba.historical_feature_models import (
    FEATURE_SCHEMA_VERSION,
    HistoricalFeatureDataset,
    HistoricalFeatureDatasetError,
    HistoricalFeatureRow,
    OpponentStatsFallback,
    PaceStatsFallback,
)
from sleeper_manager.integrations.nba.identity import PlayerMapping
from sleeper_manager.integrations.nba.official_injury_mapping import HistoricalPlayerAvailability
from sleeper_manager.integrations.nba.official_injury_models import OfficialInjuryReportSnapshot
from sleeper_manager.integrations.nba.travel import TravelContext, travel_context


def build_historical_feature_dataset(
    *,
    box_scores: Iterable[PlayerBoxScore],
    games: Iterable[ScheduledGame],
    teams: Iterable[Team],
    player_mappings: Iterable[PlayerMapping],
    injury_reports: Iterable[OfficialInjuryReportSnapshot],
    availability: Iterable[HistoricalPlayerAvailability],
    decision_cutoffs: Mapping[str, datetime] | Callable[[ScheduledGame], datetime],
    dataset_version: str,
    generated_at: datetime,
    team_box_scores: Iterable[TeamBoxScore] = (),
) -> HistoricalFeatureDataset:
    _validate_timestamp(generated_at, "generated_at")
    if not dataset_version.strip():
        raise HistoricalFeatureDatasetError("dataset_version must not be empty")

    box_score_records = tuple(box_scores)
    game_records = tuple(games)
    team_records = tuple(teams)
    report_records = tuple(injury_reports)
    availability_records = tuple(availability)
    team_box_score_records = tuple(team_box_scores)
    team_box_score_by_game_team = {
        (record.game_id, record.team_id): record for record in team_box_score_records
    }
    game_by_id = _index_games(game_records)
    team_abbreviations = _index_teams(team_records)
    sleeper_by_provider_id = _index_player_mappings(player_mappings)
    prior_by_player = _index_prior_box_scores(box_score_records)
    team_schedule = _index_team_schedule(game_records)
    availability_by_player = _index_availability(availability_records)
    report_by_game_team = _index_reports(report_records)
    report_matchups_by_date_team = _index_report_matchups(report_by_game_team)
    opponent_stats_by_game_team: dict[
        tuple[str, str],
        tuple[
            float | None,
            float | None,
            float | None,
            float | None,
            int,
            OpponentStatsFallback,
            str,
            str,
            str,
        ],
    ] = {}
    own_pace_by_game_team: dict[tuple[str, str], tuple[float | None, int, PaceStatsFallback]] = {}
    rest_by_game_team: dict[
        tuple[str, str], tuple[int | None, bool | None, SourceMetadata | None]
    ] = {}
    travel_by_game_team: dict[tuple[str, str], TravelContext] = {}
    rows: list[HistoricalFeatureRow] = []

    for box_score in box_score_records:
        game = game_by_id.get(box_score.game_id)
        if game is None:
            raise HistoricalFeatureDatasetError(
                f"Box score {box_score.game_id!r} has no matching schedule record"
            )
        cutoff = _decision_cutoff(decision_cutoffs, game)
        _validate_cutoff(cutoff, game)
        team_abbreviation = team_abbreviations.get(box_score.team_id)
        if team_abbreviation is None:
            raise HistoricalFeatureDatasetError(
                f"Box score team {box_score.team_id!r} has no team identity mapping"
            )
        opponent_team_id, opponent_abbreviation, is_home = _opponent(
            game, box_score.team_id, team_abbreviations
        )
        game_team_key = game.provider_id, box_score.team_id
        rest = rest_by_game_team.get(game_team_key)
        if rest is None:
            rest = _rest_features(game, box_score.team_id, team_schedule)
            rest_by_game_team[game_team_key] = rest
        days_rest, is_back_to_back, schedule_source = rest
        opponent_stats_key = game.provider_id, opponent_team_id
        opponent_stats = opponent_stats_by_game_team.get(opponent_stats_key)
        if opponent_stats is None:
            opponent_stats = _opponent_stats(
                game,
                opponent_team_id=opponent_team_id,
                team_box_scores=team_box_score_records,
            )
            opponent_stats_by_game_team[opponent_stats_key] = opponent_stats
        own_pace_key = game.provider_id, box_score.team_id
        own_pace = own_pace_by_game_team.get(own_pace_key)
        if own_pace is None:
            own_pace = _team_pace(
                game,
                team_id=box_score.team_id,
                team_box_scores=team_box_score_records,
            )
            own_pace_by_game_team[own_pace_key] = own_pace
        expected_pace = _expected_matchup_pace(own_pace[0], opponent_stats[3])
        exposure_pace = _baseline_exposure_pace(
            prior_by_player.get(box_score.player_id, ()),
            team_id=box_score.team_id,
            team_box_score_by_game_team=team_box_score_by_game_team,
            target_start=game.start_time,
        )
        pace_factor = (
            round(expected_pace / exposure_pace, 6)
            if expected_pace is not None and exposure_pace is not None and exposure_pace > 0
            else None
        )
        travel = travel_by_game_team.get(game_team_key)
        if travel is None:
            travel = travel_context(
                game,
                prior_games=team_schedule.get(box_score.team_id, ()),
            )
            travel_by_game_team[game_team_key] = travel
        prior = tuple(
            record
            for record in prior_by_player.get(box_score.player_id, ())
            if record.played_at is not None
            and record.played_at < game.start_time
            and _season_key(record.played_at) == _season_key(game.start_time)
        )
        latest_availability = _availability_at_cutoff(
            player_id=box_score.player_id,
            game=game,
            team_abbreviation=team_abbreviation,
            opponent_abbreviation=opponent_abbreviation,
            cutoff=cutoff,
            reports=report_by_game_team,
            report_matchups=report_matchups_by_date_team,
            availability=availability_by_player,
        )
        source_lineage = _lineage(
            box_score.source,
            *box_score.additional_sources,
            game.source,
            schedule_source,
            latest_availability[4],
        )
        rows.append(
            HistoricalFeatureRow(
                dataset_version=dataset_version,
                available_as_of=cutoff,
                player_id=box_score.player_id,
                sleeper_id=sleeper_by_provider_id.get(box_score.player_id),
                game_id=box_score.game_id,
                game_start=game.start_time,
                team_id=box_score.team_id,
                opponent_team_id=opponent_team_id,
                opponent_abbreviation=opponent_abbreviation,
                is_home=is_home,
                days_rest=days_rest,
                is_back_to_back=is_back_to_back,
                availability_status=latest_availability[0],
                availability_observation=latest_availability[1],
                availability_detail=latest_availability[2],
                availability_observed_at=latest_availability[3],
                prior_games=len(prior),
                prior_minutes_mean=_mean_minutes(prior),
                prior_minutes_last=_last_minutes(prior),
                prior_start_rate=_start_rate(prior),
                target_minutes=box_score.minutes,
                target_started=box_score.started,
                target_did_play=box_score.did_play,
                target_box_score=box_score.line,
                target_line_points=box_score.line.points,
                target_line_rebounds=box_score.line.rebounds,
                target_line_assists=box_score.line.assists,
                target_line_steals=box_score.line.steals,
                target_line_blocks=box_score.line.blocks,
                target_line_turnovers=box_score.line.turnovers,
                source_lineage=source_lineage,
                opponent_offensive_rating=opponent_stats[0],
                opponent_defensive_rating=opponent_stats[1],
                league_defensive_rating=opponent_stats[2],
                opponent_pace=opponent_stats[3],
                opponent_sample_size=opponent_stats[4],
                opponent_stats_fallback=opponent_stats[5],
                opponent_offense_band=opponent_stats[6],
                opponent_defense_band=opponent_stats[7],
                opponent_pace_band=opponent_stats[8],
                own_team_pace=own_pace[0],
                own_team_pace_sample_size=own_pace[1],
                own_team_pace_fallback=own_pace[2],
                expected_matchup_pace=expected_pace,
                baseline_exposure_pace=exposure_pace,
                pace_factor=pace_factor,
                prior_venue_id=travel.prior_venue_id,
                destination_venue_id=travel.destination_venue_id,
                travel_distance_miles=travel.distance_miles,
                time_zone_change_hours=travel.time_zone_change_hours,
                travel_direction=travel.direction,
                travel_fallback=travel.fallback,
            )
        )

    sorted_rows = tuple(sorted(rows, key=lambda row: (row.game_start, row.game_id, row.player_id)))
    return HistoricalFeatureDataset(
        dataset_version=dataset_version,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        generated_at=generated_at,
        source_versions=_source_versions(
            sorted_rows, extra_sources=(record.source for record in team_box_score_records)
        ),
        rows=sorted_rows,
    )
