from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from math import isnan
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

import httpx

from sleeper_manager.domain.nba import (
    GameStatus,
    PlayerBoxScore,
    PlayerGameFouls,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
    TeamBoxScore,
    quality_for_records,
)
from sleeper_manager.domain.scoring import BoxScoreLine

SPORTSDATAVERSE_BASE_URL = (
    "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
)


def player_box_score_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_player_boxscores/player_box_{season}.rds"


def schedule_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_schedules/nba_schedule_{season}.rds"


def team_box_score_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_team_boxscores/team_box_{season}.rds"


def play_by_play_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_pbp/play_by_play_{season}.rds"


class SportsDataverseError(RuntimeError):
    pass


class RDSReader(Protocol):
    def __call__(self, path: str) -> Mapping[str, Any]: ...


def _default_rds_reader(path: str) -> Mapping[str, Any]:
    try:
        import pyreadr  # type: ignore[import-untyped]
    except ImportError as error:
        raise SportsDataverseError(
            "SportsDataverse ingestion requires the pyreadr dependency"
        ) from error
    tables = pyreadr.read_r(path)
    if not isinstance(tables, Mapping):
        raise SportsDataverseError("SportsDataverse RDS reader returned an invalid object")
    return tables


def _clean(value: Any) -> Any:
    if isinstance(value, float) and isnan(value):
        return None
    return value


def _value(row: Mapping[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if name in row:
            return _clean(row[name])
    return default


def _required_string(row: Mapping[str, Any], names: tuple[str, ...], field: str) -> str:
    value = _value(row, names)
    if value in (None, ""):
        raise SportsDataverseError(f"Historical row is missing {field}")
    return str(value).strip()


def _datetime_value(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise SportsDataverseError(f"Historical {field} is not a valid timestamp") from error
    else:
        raise SportsDataverseError(f"Historical row is missing {field}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _int_value(value: Any) -> int:
    if value in (None, "", "NA"):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError) as error:
        raise SportsDataverseError(f"Historical statistic {value!r} is not numeric") from error


def _float_value(value: Any) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise SportsDataverseError(f"Historical minutes {value!r} is not numeric") from error


def _bool_value(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).casefold() in {"1", "true", "t", "yes", "y"}


def _period_value(value: Any, default: int) -> int:
    if isinstance(value, Mapping):
        value = value.get("number", value.get("period", value.get("value")))
    if value in (None, "", "NA"):
        return default
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _source(provider_id: str, retrieved_at: datetime) -> SourceMetadata:
    return SourceMetadata(
        provider="sportsdataverse",
        provider_id=provider_id,
        retrieved_at=retrieved_at,
        source_updated_at=None,
        schema_version="1",
    )


def _records_from_table(table: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(table, Mapping):
        return (table,)
    if hasattr(table, "to_dict"):
        rows = table.to_dict(orient="records")
    else:
        rows = table
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        raise SportsDataverseError("Historical RDS table is not row-oriented")
    items = tuple(rows)
    result = tuple(row for row in items if isinstance(row, Mapping))
    if len(result) != len(items):
        raise SportsDataverseError("Historical RDS table contains a non-object row")
    return result


def _first_table(tables: Mapping[str, Any]) -> Any:
    if not tables:
        raise SportsDataverseError("Historical RDS release did not contain a data table")
    return next(iter(tables.values()))


def _validate_columns(
    rows: tuple[Mapping[str, Any], ...], required_groups: tuple[tuple[str, tuple[str, ...]], ...]
) -> None:
    if not rows:
        return
    columns = set(rows[0])
    missing = [label for label, names in required_groups if not columns.intersection(names)]
    if missing:
        raise SportsDataverseError("Historical dataset is missing columns: " + ", ".join(missing))


def parse_player_box_score_rows(
    rows: Iterable[Mapping[str, Any]], *, retrieved_at: datetime
) -> ProviderResult[tuple[PlayerBoxScore, ...]]:
    normalized_rows = tuple(rows)
    _validate_columns(
        normalized_rows,
        (
            ("game_id", ("game_id", "gameId", "event_id")),
            ("player_id", ("player_id", "athlete_id", "athleteId")),
            ("game_date", ("game_date_time", "game_datetime", "game_date", "date")),
            ("team_id", ("team_id", "teamId")),
        ),
    )
    result: list[PlayerBoxScore] = []
    for row in normalized_rows:
        game_id = _required_string(row, ("game_id", "gameId", "event_id"), "game_id")
        player_id = _required_string(row, ("player_id", "athlete_id", "athleteId"), "player_id")
        team_id = _required_string(row, ("team_id", "teamId"), "team_id")
        played_at = _datetime_value(
            _value(row, ("game_date_time", "game_datetime", "game_date", "date")),
            "game_date",
        )
        did_not_play = _bool_value(_value(row, ("did_not_play", "didNotPlay")), False)
        line = BoxScoreLine(
            points=_int_value(_value(row, ("points", "pts"))),
            rebounds=_int_value(_value(row, ("rebounds", "reb"))),
            assists=_int_value(_value(row, ("assists", "ast"))),
            steals=_int_value(_value(row, ("steals", "stl"))),
            blocks=_int_value(_value(row, ("blocks", "blk"))),
            turnovers=_int_value(_value(row, ("turnovers", "to"))),
            three_pointers_made=_int_value(
                _value(row, ("three_pointers_made", "three_point_field_goals_made", "tpm"))
            ),
            technical_fouls=_int_value(
                _value(row, ("technical_fouls", "technical_fouls_count", "tf"))
            ),
            flagrant_fouls=_int_value(
                _value(row, ("flagrant_fouls", "flagrant_fouls_count", "ff"))
            ),
        )
        minutes = _float_value(_value(row, ("minutes", "min")))
        result.append(
            PlayerBoxScore(
                game_id=game_id,
                player_id=player_id,
                team_id=team_id,
                played_at=played_at,
                started=_bool_value(_value(row, ("started", "starter")), False),
                did_play=not did_not_play,
                minutes=None if did_not_play else minutes,
                line=line,
                source=_source(f"{game_id}:{player_id}", retrieved_at),
            )
        )
    records = tuple(result)
    return ProviderResult(
        records,
        quality_for_records(
            resource="historical_player_box_scores",
            records=records,
            retrieved_at=retrieved_at,
        ),
    )


def _historical_status(value: Any) -> GameStatus:
    text = str(value or "").casefold()
    if "cancel" in text:
        return GameStatus.CANCELED
    if "postpon" in text:
        return GameStatus.POSTPONED
    if "final" in text or text in {"post", "complete", "completed"}:
        return GameStatus.FINAL
    if "live" in text or "progress" in text:
        return GameStatus.IN_PROGRESS
    if "sched" in text or text in {"pre", "upcoming"}:
        return GameStatus.SCHEDULED
    return GameStatus.FINAL


def parse_schedule_rows(
    rows: Iterable[Mapping[str, Any]], *, retrieved_at: datetime
) -> ProviderResult[tuple[ScheduledGame, ...]]:
    normalized_rows = tuple(rows)
    _validate_columns(
        normalized_rows,
        (
            ("game_id", ("game_id", "gameId", "event_id")),
            ("game_date", ("game_date_time", "game_date", "date", "game_datetime")),
            ("home_team_id", ("home_team_id", "home_team", "home_id")),
            ("away_team_id", ("away_team_id", "away_team", "away_id")),
        ),
    )
    result: list[ScheduledGame] = []
    for row in normalized_rows:
        game_id = _required_string(row, ("game_id", "gameId", "event_id"), "game_id")
        start_time = _datetime_value(
            _value(row, ("game_date_time", "game_datetime", "game_date", "date")),
            "game_date",
        )
        home_team_id = _required_string(
            row, ("home_team_id", "home_team", "home_id"), "home_team_id"
        )
        away_team_id = _required_string(
            row, ("away_team_id", "away_team", "away_id"), "away_team_id"
        )
        result.append(
            ScheduledGame(
                provider_id=game_id,
                start_time=start_time,
                status=_historical_status(
                    _value(
                        row,
                        (
                            "status_type_name",
                            "status_type_description",
                            "status",
                            "game_status",
                        ),
                    )
                ),
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                status_detail=_value(row, ("status_detail", "description")),
                source=_source(game_id, retrieved_at),
                venue_id=_optional_string(_value(row, ("venue_id",))),
                venue_name=_optional_string(_value(row, ("venue_full_name", "venue_name"))),
                venue_city=_optional_string(_value(row, ("venue_address_city", "venue_city"))),
                venue_state=_optional_string(_value(row, ("venue_address_state", "venue_state"))),
                neutral_site=_bool_value(_value(row, ("neutral_site",)), False),
                regulation_periods=_period_value(
                    _value(row, ("format_regulation_periods", "regulation_periods")), 4
                ),
                completed_periods=_period_value(
                    _value(row, ("status_period", "completed_periods", "period")), 4
                ),
            )
        )
    records = tuple(result)
    return ProviderResult(
        records,
        quality_for_records(
            resource="historical_schedule",
            records=records,
            retrieved_at=retrieved_at,
        ),
    )


def parse_team_box_score_rows(
    rows: Iterable[Mapping[str, Any]], *, retrieved_at: datetime
) -> ProviderResult[tuple[TeamBoxScore, ...]]:
    normalized_rows = tuple(rows)
    _validate_columns(
        normalized_rows,
        (
            ("game_id", ("game_id", "gameId", "event_id")),
            ("team_id", ("team_id", "teamId")),
            ("opponent_team_id", ("opponent_team_id", "opponentTeamId")),
            ("game_date", ("game_date_time", "game_date", "date")),
            ("field_goal_attempts", ("field_goals_attempted", "fga")),
            ("free_throw_attempts", ("free_throws_attempted", "fta")),
            ("offensive_rebounds", ("offensive_rebounds", "oreb")),
            ("turnovers", ("total_turnovers", "turnovers", "to")),
        ),
    )
    result: list[TeamBoxScore] = []
    for row in normalized_rows:
        game_id = _required_string(row, ("game_id", "gameId", "event_id"), "game_id")
        team_id = _required_string(row, ("team_id", "teamId"), "team_id")
        result.append(
            TeamBoxScore(
                game_id=game_id,
                team_id=team_id,
                opponent_team_id=_required_string(
                    row, ("opponent_team_id", "opponentTeamId"), "opponent_team_id"
                ),
                played_at=_datetime_value(
                    _value(row, ("game_date_time", "game_date", "date")), "game_date"
                ),
                points=_int_value(_value(row, ("team_score", "points"))),
                opponent_points=_int_value(_value(row, ("opponent_team_score", "opponent_points"))),
                field_goal_attempts=_int_value(_value(row, ("field_goals_attempted", "fga"))),
                free_throw_attempts=_int_value(_value(row, ("free_throws_attempted", "fta"))),
                offensive_rebounds=_int_value(_value(row, ("offensive_rebounds", "oreb"))),
                turnovers=_int_value(_value(row, ("total_turnovers", "turnovers", "to"))),
                source=_source(f"{game_id}:{team_id}", retrieved_at),
                regulation_periods=_period_value(
                    _value(row, ("format_regulation_periods", "regulation_periods")), 4
                ),
                completed_periods=_period_value(
                    _value(row, ("status_period", "completed_periods", "period")), 4
                ),
            )
        )
    records = tuple(result)
    return ProviderResult(
        records,
        quality_for_records(
            resource="historical_team_box_scores",
            records=records,
            retrieved_at=retrieved_at,
        ),
    )


def parse_play_by_play_foul_rows(
    rows: Iterable[Mapping[str, Any]], *, retrieved_at: datetime
) -> ProviderResult[tuple[PlayerGameFouls, ...]]:
    normalized_rows = tuple(rows)
    _validate_columns(
        normalized_rows,
        (
            ("game_id", ("game_id", "gameId", "event_id")),
            ("play_type", ("type_text", "typeText")),
            ("athlete_id", ("athlete_id_1", "athleteId1")),
        ),
    )
    counts: dict[tuple[str, str], list[int]] = {}
    for row in normalized_rows:
        play_type = str(_value(row, ("type_text", "typeText"), "")).casefold()
        is_technical = "technical" in play_type and "free throw" not in play_type
        is_flagrant = "flagrant foul" in play_type and "free throw" not in play_type
        if not is_technical and not is_flagrant:
            continue
        game_id = _required_string(row, ("game_id", "gameId", "event_id"), "game_id")
        athlete_ids = {
            _optional_string(_value(row, names))
            for names in (
                ("athlete_id_1", "athleteId1"),
                ("athlete_id_2", "athleteId2"),
                ("athlete_id_3", "athleteId3"),
            )
        }
        for player_id in athlete_ids:
            if player_id is None:
                continue
            key = game_id, player_id
            totals = counts.setdefault(key, [0, 0])
            totals[0] += int(is_technical)
            totals[1] += int(is_flagrant)
    records = tuple(
        PlayerGameFouls(
            game_id=game_id,
            player_id=player_id,
            technical_fouls=totals[0],
            flagrant_fouls=totals[1],
            source=_source(f"{game_id}:{player_id}:fouls", retrieved_at),
        )
        for (game_id, player_id), totals in sorted(counts.items())
    )
    return ProviderResult(
        records,
        quality_for_records(
            resource="historical_player_game_fouls",
            records=records,
            retrieved_at=retrieved_at,
        ),
    )


def apply_player_game_fouls(
    box_scores: Iterable[PlayerBoxScore], fouls: Iterable[PlayerGameFouls]
) -> tuple[PlayerBoxScore, ...]:
    by_key = {(record.game_id, record.player_id): record for record in fouls}
    result: list[PlayerBoxScore] = []
    for box_score in box_scores:
        foul = by_key.get((box_score.game_id, box_score.player_id))
        if foul is None:
            result.append(box_score)
            continue
        result.append(
            replace(
                box_score,
                line=replace(
                    box_score.line,
                    technical_fouls=foul.technical_fouls,
                    flagrant_fouls=foul.flagrant_fouls,
                ),
                additional_sources=box_score.additional_sources + (foul.source,),
            )
        )
    return tuple(result)


class SportsDataverseClient:
    """Batch adapter for the public SportsDataverse RDS releases."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        rds_reader: RDSReader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=60)
        self._owns_client = client is None
        self._rds_reader = rds_reader or _default_rds_reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __aenter__(self) -> "SportsDataverseClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _download_rows(self, url: str) -> tuple[Mapping[str, Any], ...]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise SportsDataverseError(f"SportsDataverse request failed for {url}") from error
        with NamedTemporaryFile(suffix=".rds") as file:
            file.write(response.content)
            file.flush()
            try:
                tables = self._rds_reader(file.name)
            except SportsDataverseError:
                raise
            except Exception as error:
                raise SportsDataverseError(
                    f"Could not parse SportsDataverse release {url}"
                ) from error
        return _records_from_table(_first_table(tables))

    async def player_box_scores(self, season: int) -> ProviderResult[tuple[PlayerBoxScore, ...]]:
        retrieved_at = self._clock()
        return parse_player_box_score_rows(
            await self._download_rows(player_box_score_url(season)),
            retrieved_at=retrieved_at,
        )

    async def schedule(self, season: int) -> ProviderResult[tuple[ScheduledGame, ...]]:
        retrieved_at = self._clock()
        return parse_schedule_rows(
            await self._download_rows(schedule_url(season)),
            retrieved_at=retrieved_at,
        )

    async def team_box_scores(self, season: int) -> ProviderResult[tuple[TeamBoxScore, ...]]:
        retrieved_at = self._clock()
        return parse_team_box_score_rows(
            await self._download_rows(team_box_score_url(season)),
            retrieved_at=retrieved_at,
        )

    async def player_game_fouls(self, season: int) -> ProviderResult[tuple[PlayerGameFouls, ...]]:
        retrieved_at = self._clock()
        return parse_play_by_play_foul_rows(
            await self._download_rows(play_by_play_url(season)),
            retrieved_at=retrieved_at,
        )
