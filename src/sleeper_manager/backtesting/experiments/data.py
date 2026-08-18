from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sleeper_manager.domain.nba import (
    GameStatus,
    PlayerBoxScore,
    ProviderPlayer,
    ScheduledGame,
    SourceMetadata,
    Team,
    TeamBoxScore,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import FEATURE_SCHEMA_VERSION
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.sportsdataverse import (
    apply_player_game_fouls,
    parse_play_by_play_foul_rows,
    parse_player_box_score_rows,
    parse_schedule_rows,
    parse_team_box_score_rows,
    play_by_play_url,
    player_box_score_url,
    schedule_url,
    team_box_score_url,
)


class ExperimentDataError(RuntimeError):
    """Raised when historical experiment data cannot be assembled."""

    pass


class RDSReader(Protocol):
    def __call__(self, path: str) -> Mapping[str, Any]: ...


_PLAY_BY_PLAY_COLUMNS = (
    "game_id",
    "gameId",
    "event_id",
    "type_text",
    "typeText",
    "athlete_id_1",
    "athleteId1",
    "athlete_id_2",
    "athleteId2",
    "athlete_id_3",
    "athleteId3",
)


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    season: int
    resource: str
    path: str
    url: str
    sha256: str
    byte_count: int
    row_count: int


@dataclass(frozen=True, slots=True)
class HistoricalExperimentInputs:
    games: tuple[ScheduledGame, ...]
    player_box_scores: tuple[PlayerBoxScore, ...]
    team_box_scores: tuple[TeamBoxScore, ...]
    teams: tuple[Team, ...]
    provider_players: tuple[ProviderPlayer, ...]
    artifacts: tuple[SourceArtifact, ...]
    excluded_player_rows: int


def scoring_policy_from_league_fixture(path: Path) -> ScoringPolicy:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ExperimentDataError(f"Could not read league fixture {path}") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("scoring_settings"), Mapping):
        raise ExperimentDataError("League fixture has no scoring_settings object")
    return ScoringPolicy.from_sleeper(payload["scoring_settings"])


def decision_cutoff(game: ScheduledGame) -> datetime:
    return game.start_time - timedelta(minutes=30)


def load_historical_experiment_inputs(
    raw_dir: Path,
    *,
    seasons: Iterable[int] = (2023, 2024, 2025, 2026),
    retrieved_at: datetime | None = None,
    rds_reader: RDSReader | None = None,
) -> HistoricalExperimentInputs:
    timestamp = retrieved_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ExperimentDataError("Historical retrieval timestamp must be timezone-aware")
    reader = rds_reader or _default_rds_reader
    games: list[ScheduledGame] = []
    player_box_scores: list[PlayerBoxScore] = []
    team_box_scores: list[TeamBoxScore] = []
    teams: dict[str, Team] = {}
    players: dict[tuple[str, str], ProviderPlayer] = {}
    artifacts: list[SourceArtifact] = []
    excluded_player_rows = 0

    for season in seasons:
        season_dir = raw_dir / str(season)
        schedule_rows, artifact = _load_resource(
            season_dir / f"nba_schedule_{season}.rds",
            season=season,
            resource="schedule",
            url=schedule_url(season),
            reader=reader,
        )
        artifacts.append(artifact)
        regular_schedule_rows = _regular_schedule_rows(schedule_rows)
        parsed_games = tuple(
            game
            for game in parse_schedule_rows(
                regular_schedule_rows,
                retrieved_at=timestamp,
            ).records
            if game.status is GameStatus.FINAL
        )
        game_ids = {game.provider_id for game in parsed_games}
        games.extend(parsed_games)

        player_rows, artifact = _load_resource(
            season_dir / f"player_box_{season}.rds",
            season=season,
            resource="player_box_scores",
            url=player_box_score_url(season),
            reader=reader,
        )
        artifacts.append(artifact)
        candidate_player_rows = tuple(
            row
            for row in _regular_season_rows(player_rows)
            if _string(row.get("game_id")) in game_ids
        )
        regular_player_rows = tuple(
            row for row in candidate_player_rows if _optional_string(row.get("athlete_id"))
        )
        excluded_player_rows += len(candidate_player_rows) - len(regular_player_rows)
        parsed_box_scores = parse_player_box_score_rows(
            regular_player_rows,
            retrieved_at=timestamp,
        ).records

        team_rows, artifact = _load_resource(
            season_dir / f"team_box_{season}.rds",
            season=season,
            resource="team_box_scores",
            url=team_box_score_url(season),
            reader=reader,
        )
        artifacts.append(artifact)
        regular_team_rows = tuple(
            row
            for row in _regular_season_rows(team_rows)
            if _string(row.get("game_id")) in game_ids
        )
        schedule_by_game_id = {game.provider_id: game for game in parsed_games}
        parsed_team_box_scores = parse_team_box_score_rows(
            regular_team_rows,
            retrieved_at=timestamp,
        ).records
        for team_box_score in parsed_team_box_scores:
            schedule = schedule_by_game_id.get(team_box_score.game_id)
            if schedule is None:
                raise ExperimentDataError(
                    f"Team box score {team_box_score.game_id!r} has no matching schedule record"
                )
            team_box_scores.append(
                replace(
                    team_box_score,
                    regulation_periods=schedule.regulation_periods,
                    completed_periods=schedule.completed_periods,
                )
            )

        play_rows, artifact = _load_resource(
            season_dir / f"play_by_play_{season}.rds",
            season=season,
            resource="play_by_play",
            url=play_by_play_url(season),
            reader=reader,
            columns=_PLAY_BY_PLAY_COLUMNS,
        )
        artifacts.append(artifact)
        relevant_play_rows = tuple(
            row for row in play_rows if _string(row.get("game_id")) in game_ids
        )
        fouls = parse_play_by_play_foul_rows(
            relevant_play_rows,
            retrieved_at=timestamp,
        ).records
        player_box_scores.extend(apply_player_game_fouls(parsed_box_scores, fouls))
        _collect_identities(
            regular_player_rows,
            retrieved_at=timestamp,
            teams=teams,
            players=players,
        )

    _validate_unique_games(games)
    _validate_box_score_games(player_box_scores, games)
    return HistoricalExperimentInputs(
        games=tuple(sorted(games, key=lambda game: (game.start_time, game.provider_id))),
        player_box_scores=tuple(
            sorted(
                player_box_scores,
                key=lambda row: (
                    row.played_at or datetime.min.replace(tzinfo=UTC),
                    row.game_id,
                    row.player_id,
                ),
            )
        ),
        team_box_scores=tuple(
            sorted(team_box_scores, key=lambda row: (row.played_at, row.game_id, row.team_id))
        ),
        teams=tuple(sorted(teams.values(), key=lambda team: team.provider_id)),
        provider_players=tuple(
            sorted(players.values(), key=lambda player: (player.provider_id, player.team_id or ""))
        ),
        artifacts=tuple(artifacts),
        excluded_player_rows=excluded_player_rows,
    )


def dataset_version_for(
    artifacts: Iterable[SourceArtifact],
    *,
    scoring_policy: ScoringPolicy,
    injury_hashes: Iterable[str] = (),
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
) -> str:
    payload = {
        "artifacts": [
            {
                "season": artifact.season,
                "resource": artifact.resource,
                "sha256": artifact.sha256,
            }
            for artifact in sorted(artifacts, key=lambda value: (value.season, value.resource))
        ],
        "injury_hashes": sorted(injury_hashes),
        "scoring_policy_version": scoring_policy.version,
        "feature_schema_version": feature_schema_version,
        "decision_cutoff_minutes": 30,
        "target_semantics": "reconstructed_sleeper_policy",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return (
        f"historical-features-v{feature_schema_version}-{hashlib.sha256(encoded).hexdigest()[:16]}"
    )


def artifact_manifest(artifacts: Iterable[SourceArtifact]) -> list[dict[str, object]]:
    return [
        {
            "season": artifact.season,
            "resource": artifact.resource,
            "path": artifact.path,
            "url": artifact.url,
            "sha256": artifact.sha256,
            "byte_count": artifact.byte_count,
            "row_count": artifact.row_count,
        }
        for artifact in sorted(artifacts, key=lambda value: (value.season, value.resource))
    ]


def _default_rds_reader(path: str) -> Mapping[str, Any]:
    try:
        pyreadr = importlib.import_module("pyreadr")
    except ImportError as error:
        raise ExperimentDataError("Historical experiment ingestion requires pyreadr") from error
    tables = pyreadr.read_r(path)
    if not isinstance(tables, Mapping):
        raise ExperimentDataError(f"RDS reader returned an invalid value for {path}")
    return tables


def _load_resource(
    path: Path,
    *,
    season: int,
    resource: str,
    url: str,
    reader: RDSReader,
    columns: tuple[str, ...] | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], SourceArtifact]:
    if not path.is_file():
        raise ExperimentDataError(f"Missing historical source artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        tables = reader(str(path))
    except Exception as error:
        raise ExperimentDataError(f"Could not parse historical source artifact: {path}") from error
    if not tables:
        raise ExperimentDataError(f"Historical source artifact has no table: {path}")
    table = next(iter(tables.values()))
    if not hasattr(table, "to_dict"):
        raise ExperimentDataError(f"Historical source table is not row-oriented: {path}")
    row_count = len(table) if hasattr(table, "__len__") else None
    if columns is not None and hasattr(table, "columns") and hasattr(table, "__getitem__"):
        available = set(table.columns)
        selected = [column for column in columns if column in available]
        table = table[selected]
    values = table.to_dict(orient="records")
    if not isinstance(values, list) or not all(isinstance(row, Mapping) for row in values):
        raise ExperimentDataError(f"Historical source table has invalid rows: {path}")
    rows = tuple(values)
    return rows, SourceArtifact(
        season=season,
        resource=resource,
        path=str(path),
        url=url,
        sha256=digest.hexdigest(),
        byte_count=path.stat().st_size,
        row_count=row_count if row_count is not None else len(rows),
    )


def _regular_season_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return tuple(row for row in rows if _integer(row.get("season_type")) == 2)


def _regular_schedule_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    nba_team_ids = {str(value) for value in range(1, 31)}
    return tuple(
        row
        for row in _regular_season_rows(rows)
        if _string(row.get("home_id")) in nba_team_ids
        and _string(row.get("away_id")) in nba_team_ids
        and "championship" not in _string(row.get("notes_headline")).casefold()
    )


def _collect_identities(
    rows: Iterable[Mapping[str, Any]],
    *,
    retrieved_at: datetime,
    teams: dict[str, Team],
    players: dict[tuple[str, str], ProviderPlayer],
) -> None:
    for row in rows:
        team_id = _required_string(row, "team_id")
        abbreviation = normalize_team(_required_string(row, "team_abbreviation"))
        if abbreviation is None:
            raise ExperimentDataError(f"Could not normalize team abbreviation for {team_id}")
        source = SourceMetadata(
            provider="sportsdataverse",
            provider_id=team_id,
            retrieved_at=retrieved_at,
            schema_version="1",
        )
        team = Team(
            provider_id=team_id,
            abbreviation=abbreviation,
            name=_required_string(row, "team_display_name"),
            location=_optional_string(row.get("team_location")),
            source=source,
        )
        previous_team = teams.get(team_id)
        if previous_team is not None and previous_team.abbreviation != team.abbreviation:
            raise ExperimentDataError(f"Conflicting team abbreviation for provider ID {team_id}")
        teams[team_id] = team

        player_id = _required_string(row, "athlete_id")
        key = player_id, abbreviation
        players[key] = ProviderPlayer(
            provider_id=player_id,
            full_name=_required_string(row, "athlete_display_name"),
            team_id=team_id,
            team_abbreviation=abbreviation,
            active=_boolean(row.get("active")),
            source=SourceMetadata(
                provider="sportsdataverse",
                provider_id=player_id,
                retrieved_at=retrieved_at,
                schema_version="1",
            ),
        )


def _validate_unique_games(games: Iterable[ScheduledGame]) -> None:
    seen: set[str] = set()
    for game in games:
        if game.provider_id in seen:
            raise ExperimentDataError(f"Duplicate regular-season game {game.provider_id}")
        seen.add(game.provider_id)


def _validate_box_score_games(
    box_scores: Iterable[PlayerBoxScore], games: Iterable[ScheduledGame]
) -> None:
    game_ids = {game.provider_id for game in games}
    missing = sorted({row.game_id for row in box_scores if row.game_id not in game_ids})
    if missing:
        raise ExperimentDataError(
            f"Player box scores reference {len(missing)} games outside the regular-season schedule"
        )


def _required_string(row: Mapping[str, Any], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise ExperimentDataError(f"Historical identity row is missing {field}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.casefold() != "nan" else None


def _string(value: Any) -> str:
    return _optional_string(value) or ""


def _integer(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None
