from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    GameStatus,
    PlayerBoxScore,
    ScheduledGame,
    SourceMetadata,
    Team,
    TeamBoxScore,
)
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    DatasetSourceVersion,
    HistoricalFeatureDatasetError,
    HistoricalFeatureRow,
    OpponentStatsFallback,
    PaceStatsFallback,
)
from sleeper_manager.integrations.nba.identity import PlayerMapping
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.official_injury_mapping import HistoricalPlayerAvailability
from sleeper_manager.integrations.nba.official_injury_models import (
    EASTERN_TIME,
    OfficialInjuryReportSnapshot,
    ReportSubmissionStatus,
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
        and nba_season_start_year(record.played_at) == nba_season_start_year(game.start_time)
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
        record
        for record in prior
        if nba_season_start_year(record.played_at) == nba_season_start_year(game.start_time)
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
    team_box_score_by_game_team: Mapping[tuple[str, str], TeamBoxScore],
    target_start: datetime,
) -> float | None:
    values: list[float] = []
    for player_game in prior_player_games:
        if player_game.played_at is None or player_game.played_at >= target_start:
            continue
        team_game = team_box_score_by_game_team.get((player_game.game_id, team_id))
        if team_game is None or team_game.played_at >= target_start or team_game.pace_48 <= 0:
            continue
        values.append(team_game.pace_48)
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
