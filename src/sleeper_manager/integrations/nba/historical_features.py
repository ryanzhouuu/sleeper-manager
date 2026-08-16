from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    GameStatus,
    PlayerBoxScore,
    ScheduledGame,
    SourceMetadata,
    Team,
    TeamBoxScore,
)
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.identity import PlayerMapping
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.official_injury_mapping import HistoricalPlayerAvailability
from sleeper_manager.integrations.nba.official_injury_report import (
    EASTERN_TIME,
    OfficialInjuryReportSnapshot,
    ReportSubmissionStatus,
)
from sleeper_manager.integrations.nba.travel import TravelContext, travel_context

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
            team_box_scores=team_box_score_records,
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


def _validate_timestamp(value: datetime, field: str) -> None:
    if value.tzinfo is None:
        raise HistoricalFeatureDatasetError(f"{field} must be timezone-aware")


def _validate_cutoff(cutoff: datetime, game: ScheduledGame) -> None:
    _validate_timestamp(cutoff, f"decision cutoff for {game.provider_id}")
    if cutoff > game.start_time:
        raise HistoricalFeatureDatasetError(
            f"Decision cutoff for {game.provider_id!r} is after game start"
        )


def _decision_cutoff(
    cutoffs: Mapping[str, datetime] | Callable[[ScheduledGame], datetime], game: ScheduledGame
) -> datetime:
    if callable(cutoffs):
        return cutoffs(game)
    try:
        return cutoffs[game.provider_id]
    except KeyError as error:
        raise HistoricalFeatureDatasetError(
            f"No decision cutoff supplied for game {game.provider_id!r}"
        ) from error


def _index_games(games: Iterable[ScheduledGame]) -> dict[str, ScheduledGame]:
    result: dict[str, ScheduledGame] = {}
    for game in games:
        if game.provider_id in result:
            raise HistoricalFeatureDatasetError(f"Duplicate schedule game {game.provider_id!r}")
        result[game.provider_id] = game
    return result


def _index_teams(teams: Iterable[Team]) -> dict[str, str]:
    result: dict[str, str] = {}
    for team in teams:
        abbreviation = normalize_team(team.abbreviation)
        if abbreviation is None:
            raise HistoricalFeatureDatasetError(f"Team {team.provider_id!r} has no abbreviation")
        if team.provider_id in result and result[team.provider_id] != abbreviation:
            raise HistoricalFeatureDatasetError(
                f"Conflicting identity for team {team.provider_id!r}"
            )
        result[team.provider_id] = abbreviation
    return result


def _index_player_mappings(mappings: Iterable[PlayerMapping]) -> dict[str, str]:
    result: dict[str, str] = {}
    for mapping in mappings:
        if mapping.espn_id is None:
            continue
        previous = result.get(mapping.espn_id)
        if previous is not None and previous != mapping.sleeper_id:
            raise HistoricalFeatureDatasetError(
                f"Conflicting Sleeper identities for provider player {mapping.espn_id!r}"
            )
        result[mapping.espn_id] = mapping.sleeper_id
    return result


def _index_prior_box_scores(
    box_scores: Iterable[PlayerBoxScore],
) -> dict[str, tuple[PlayerBoxScore, ...]]:
    result: dict[str, list[PlayerBoxScore]] = defaultdict(list)
    for box_score in box_scores:
        result[box_score.player_id].append(box_score)
    return {
        player_id: tuple(sorted(records, key=_box_score_sort_key))
        for player_id, records in result.items()
    }


def _box_score_sort_key(record: PlayerBoxScore) -> datetime:
    return record.played_at or datetime.min.replace(tzinfo=UTC)


def _index_team_schedule(
    games: Iterable[ScheduledGame],
) -> dict[str, tuple[ScheduledGame, ...]]:
    result: dict[str, list[ScheduledGame]] = defaultdict(list)
    for game in games:
        if game.status is not GameStatus.FINAL:
            continue
        result[game.home_team_id].append(game)
        result[game.away_team_id].append(game)
    return {
        team_id: tuple(sorted(records, key=lambda game: game.start_time))
        for team_id, records in result.items()
    }


def _index_availability(
    records: Iterable[HistoricalPlayerAvailability],
) -> dict[str, tuple[HistoricalPlayerAvailability, ...]]:
    result: dict[str, list[HistoricalPlayerAvailability]] = defaultdict(list)
    for record in records:
        result[record.player_id].append(record)
    return {
        player_id: tuple(sorted(items, key=lambda record: record.available_as_of))
        for player_id, items in result.items()
    }


def _index_reports(
    reports: Iterable[OfficialInjuryReportSnapshot],
) -> dict[tuple[date, str, str], tuple[OfficialInjuryReportSnapshot, ...]]:
    result: dict[tuple[date, str, str], list[OfficialInjuryReportSnapshot]] = defaultdict(list)
    for report in reports:
        for status in report.team_statuses:
            key = (status.game_date, status.matchup, status.team_abbreviation)
            result[key].append(report)
    return {
        key: tuple(sorted(items, key=lambda report: report.published_at))
        for key, items in result.items()
    }


def _index_report_matchups(
    reports: Mapping[tuple[date, str, str], tuple[OfficialInjuryReportSnapshot, ...]],
) -> dict[tuple[date, str], tuple[str, ...]]:
    result: dict[tuple[date, str], set[str]] = defaultdict(set)
    for game_date, matchup, team_abbreviation in reports:
        result[(game_date, team_abbreviation)].add(matchup)
    return {key: tuple(sorted(matchups)) for key, matchups in result.items()}


def _opponent(
    game: ScheduledGame,
    team_id: str,
    team_abbreviations: Mapping[str, str],
) -> tuple[str, str, bool]:
    if team_id == game.home_team_id:
        opponent_team_id = game.away_team_id
        is_home = True
    elif team_id == game.away_team_id:
        opponent_team_id = game.home_team_id
        is_home = False
    else:
        raise HistoricalFeatureDatasetError(
            f"Team {team_id!r} is not part of scheduled game {game.provider_id!r}"
        )
    opponent_abbreviation = team_abbreviations.get(opponent_team_id)
    if opponent_abbreviation is None:
        raise HistoricalFeatureDatasetError(
            f"Opponent team {opponent_team_id!r} has no team identity mapping"
        )
    return opponent_team_id, opponent_abbreviation, is_home


def _rest_features(
    game: ScheduledGame,
    team_id: str,
    team_schedule: Mapping[str, tuple[ScheduledGame, ...]],
) -> tuple[int | None, bool | None, SourceMetadata | None]:
    previous = [
        scheduled
        for scheduled in team_schedule.get(team_id, ())
        if scheduled.start_time < game.start_time
    ]
    if not previous:
        return None, None, None
    previous_game = previous[-1]
    date_difference = (_local_game_date(game) - _local_game_date(previous_game)).days
    return max(date_difference - 1, 0), date_difference <= 1, previous_game.source


def _opponent_stats(
    game: ScheduledGame,
    *,
    opponent_team_id: str,
    team_box_scores: tuple[TeamBoxScore, ...],
    lookback_games: int = 10,
    shrinkage_games: float = 5.0,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    int,
    OpponentStatsFallback,
    str,
    str,
    str,
]:
    prior = tuple(
        record
        for record in team_box_scores
        if record.played_at < game.start_time
        and _season_key(record.played_at) == _season_key(game.start_time)
        and record.estimated_possessions > 0
    )
    if not prior:
        return (
            None,
            None,
            None,
            None,
            0,
            OpponentStatsFallback.MISSING,
            "unknown",
            "unknown",
            "unknown",
        )
    opponent_prior = tuple(record for record in prior if record.team_id == opponent_team_id)[
        -lookback_games:
    ]
    league_offenses = tuple(record.points / record.estimated_possessions * 100 for record in prior)
    league_defenses = tuple(
        record.opponent_points / record.estimated_possessions * 100 for record in prior
    )
    league_paces = tuple(record.pace_48 for record in prior)
    league_offense = _mean(league_offenses)
    league_defense = _mean(league_defenses)
    league_pace = _mean(league_paces)
    if not opponent_prior:
        return (
            round(league_offense, 6),
            round(league_defense, 6),
            round(league_defense, 6),
            round(league_pace, 6),
            0,
            OpponentStatsFallback.LEAGUE_AVERAGE,
            _value_band(league_offense, league_offenses),
            _value_band(league_defense, league_defenses),
            _value_band(league_pace, league_paces),
        )
    sample_size = len(opponent_prior)
    weight = sample_size / (sample_size + shrinkage_games)
    offense = _mean(record.points / record.estimated_possessions * 100 for record in opponent_prior)
    defense = _mean(
        record.opponent_points / record.estimated_possessions * 100 for record in opponent_prior
    )
    pace = _mean(record.pace_48 for record in opponent_prior)
    fallback = (
        OpponentStatsFallback.OBSERVED
        if sample_size >= lookback_games
        else OpponentStatsFallback.SHRUNK
    )
    adjusted_offense = league_offense + weight * (offense - league_offense)
    adjusted_defense = league_defense + weight * (defense - league_defense)
    adjusted_pace = league_pace + weight * (pace - league_pace)
    return (
        round(adjusted_offense, 6),
        round(adjusted_defense, 6),
        round(league_defense, 6),
        round(adjusted_pace, 6),
        sample_size,
        fallback,
        _value_band(adjusted_offense, league_offenses),
        _value_band(adjusted_defense, league_defenses),
        _value_band(adjusted_pace, league_paces),
    )


def _team_pace(
    game: ScheduledGame,
    *,
    team_id: str,
    team_box_scores: tuple[TeamBoxScore, ...],
    lookback_games: int = 10,
    shrinkage_games: float = 10.0,
) -> tuple[float | None, int, PaceStatsFallback]:
    prior = tuple(
        record
        for record in team_box_scores
        if record.team_id == team_id and record.played_at < game.start_time and record.pace_48 > 0
    )
    same_season = tuple(
        record for record in prior if _season_key(record.played_at) == _season_key(game.start_time)
    )
    league_prior = tuple(
        record
        for record in team_box_scores
        if record.played_at < game.start_time and record.pace_48 > 0
    )
    if not league_prior:
        return None, 0, PaceStatsFallback.MISSING
    league_mean = _mean(record.pace_48 for record in league_prior)
    if not same_season:
        prior_season = prior[-lookback_games:]
        if not prior_season:
            return round(league_mean, 6), 0, PaceStatsFallback.LEAGUE_AVERAGE
        return (
            round(_mean(record.pace_48 for record in prior_season), 6),
            0,
            PaceStatsFallback.PRIOR_SEASON,
        )
    observed = same_season[-lookback_games:]
    observed_mean = _mean(record.pace_48 for record in observed)
    weight = len(observed) / (len(observed) + shrinkage_games)
    fallback = (
        PaceStatsFallback.OBSERVED if len(observed) >= lookback_games else PaceStatsFallback.SHRUNK
    )
    return round(league_mean + weight * (observed_mean - league_mean), 6), len(observed), fallback


def _expected_matchup_pace(
    own_team_pace: float | None, opponent_pace: float | None
) -> float | None:
    if own_team_pace is None or opponent_pace is None:
        return None
    return round((own_team_pace + opponent_pace) / 2, 6)


def _baseline_exposure_pace(
    prior_player_games: Iterable[PlayerBoxScore],
    *,
    team_id: str,
    team_box_scores: tuple[TeamBoxScore, ...],
    target_start: datetime,
) -> float | None:
    paces_by_game = {
        record.game_id: record.pace_48
        for record in team_box_scores
        if record.team_id == team_id and record.played_at < target_start and record.pace_48 > 0
    }
    values = tuple(
        paces_by_game[record.game_id]
        for record in prior_player_games
        if record.game_id in paces_by_game
        and record.played_at is not None
        and record.played_at < target_start
    )
    return round(_mean(values), 6) if values else None


def _mean(values: Iterable[float]) -> float:
    records = tuple(values)
    if not records:
        raise HistoricalFeatureDatasetError("Cannot average an empty feature group")
    return sum(records) / len(records)


def _value_band(value: float, population: tuple[float, ...]) -> str:
    ordered = tuple(sorted(population))
    if value < ordered[len(ordered) // 3]:
        return "low"
    if value < ordered[(2 * len(ordered)) // 3]:
        return "medium"
    return "high"


def _season_key(value: datetime) -> int:
    return value.year if value.month >= 10 else value.year - 1


def _mean_minutes(records: tuple[PlayerBoxScore, ...]) -> float | None:
    minutes = tuple(record.minutes for record in records if record.minutes is not None)
    return sum(minutes) / len(minutes) if minutes else None


def _last_minutes(records: tuple[PlayerBoxScore, ...]) -> float | None:
    for record in reversed(records):
        if record.minutes is not None:
            return record.minutes
    return None


def _start_rate(records: tuple[PlayerBoxScore, ...]) -> float | None:
    appearances = tuple(record for record in records if record.did_play)
    return sum(record.started for record in appearances) / len(appearances) if appearances else None


def _availability_at_cutoff(
    *,
    player_id: str,
    game: ScheduledGame,
    team_abbreviation: str,
    opponent_abbreviation: str,
    cutoff: datetime,
    reports: Mapping[tuple[date, str, str], tuple[OfficialInjuryReportSnapshot, ...]],
    report_matchups: Mapping[tuple[date, str], tuple[str, ...]],
    availability: Mapping[str, tuple[HistoricalPlayerAvailability, ...]],
) -> tuple[
    AvailabilityStatus,
    AvailabilityObservation,
    str | None,
    datetime | None,
    SourceMetadata | None,
]:
    game_date = _local_game_date(game)
    matchup = _matchup(game, team_abbreviation, opponent_abbreviation, report_matchups)
    if matchup is None:
        return (
            AvailabilityStatus.UNKNOWN,
            AvailabilityObservation.MISSING_REPORT,
            "No official injury report was available by the decision cutoff",
            None,
            None,
        )
    report_candidates = tuple(
        report
        for report in reports.get((game_date, matchup, team_abbreviation), ())
        if report.published_at <= cutoff
    )
    if not report_candidates:
        return (
            AvailabilityStatus.UNKNOWN,
            AvailabilityObservation.MISSING_REPORT,
            "No official injury report was available by the decision cutoff",
            None,
            None,
        )
    report = report_candidates[-1]
    team_status = next(
        status
        for status in report.team_statuses
        if status.game_date == game_date
        and status.matchup == matchup
        and status.team_abbreviation == team_abbreviation
    )
    if team_status.status is ReportSubmissionStatus.NOT_YET_SUBMITTED:
        return (
            AvailabilityStatus.UNKNOWN,
            AvailabilityObservation.TEAM_NOT_YET_SUBMITTED,
            "Team injury report was not yet submitted by the decision cutoff",
            report.published_at,
            report.source,
        )
    matching = tuple(
        record
        for record in availability.get(player_id, ())
        if record.available_as_of == report.published_at
        and record.game_date == game_date
        and record.matchup == matchup
        and normalize_team(record.team_abbreviation) == team_abbreviation
    )
    if matching:
        record = matching[-1]
        return (
            record.status,
            AvailabilityObservation.REPORTED,
            record.detail,
            record.available_as_of,
            record.source,
        )
    return (
        AvailabilityStatus.UNKNOWN,
        AvailabilityObservation.NOT_LISTED,
        "Player was not listed in the latest submitted official injury report",
        report.published_at,
        report.source,
    )


def _matchup(
    game: ScheduledGame,
    team_abbreviation: str,
    opponent_abbreviation: str,
    reports: Mapping[tuple[date, str], tuple[str, ...]],
) -> str | None:
    candidate_matchups = reports.get((_local_game_date(game), team_abbreviation), ())
    expected_teams = {team_abbreviation, opponent_abbreviation}
    matchups = {
        matchup
        for matchup in candidate_matchups
        if {normalize_team(value) for value in matchup.split("@")} == expected_teams
    }
    if not matchups:
        return None
    if len(matchups) == 1:
        return next(iter(matchups))
    raise HistoricalFeatureDatasetError(
        f"Could not resolve official-report matchup for scheduled game {game.provider_id!r}"
    )


def _local_game_date(game: ScheduledGame) -> date:
    return game.start_time.astimezone(EASTERN_TIME).date()


def _lineage(
    *sources: SourceMetadata | None,
) -> tuple[SourceMetadata, ...]:
    result: list[SourceMetadata] = []
    seen: set[tuple[str, str, str | None]] = set()
    for source in sources:
        if source is None:
            continue
        key = (source.provider, source.provider_id, source.content_hash)
        if key not in seen:
            seen.add(key)
            result.append(source)
    return tuple(result)


def _source_versions(
    rows: Iterable[HistoricalFeatureRow],
    *,
    extra_sources: Iterable[SourceMetadata] = (),
) -> tuple[DatasetSourceVersion, ...]:
    by_provider: dict[str, set[str]] = defaultdict(set)
    source_ids: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for source in row.source_lineage:
            by_provider[source.provider].add(source.schema_version)
            source_ids[source.provider].add(source.provider_id)
    for source in extra_sources:
        by_provider[source.provider].add(source.schema_version)
        source_ids[source.provider].add(source.provider_id)
    return tuple(
        DatasetSourceVersion(
            provider=provider,
            schema_version=",".join(sorted(versions)),
            source_ids=tuple(sorted(source_ids[provider])),
        )
        for provider, versions in sorted(by_provider.items())
    )
