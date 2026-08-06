from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    DataQualityReport,
    GameStatus,
    GameSummary,
    PlayerAvailability,
    PlayerBoxScore,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
    quality_for_records,
)
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.schemas import (
    ESPNGameSummaryPayload,
    ESPNInjuriesPayload,
    ESPNScoreboardPayload,
    ESPNTeamRosterPayload,
    ESPNTeamSchedulePayload,
)


class ESPNAPIError(RuntimeError):
    pass


class ESPNSchemaError(ESPNAPIError):
    pass


_RESOURCE_TTLS = {
    "scoreboard": timedelta(minutes=2),
    "game_summary": timedelta(minutes=1),
    "injuries": timedelta(minutes=15),
    "team_roster": timedelta(hours=12),
    "team_schedule": timedelta(hours=12),
}


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ESPNSchemaError(f"ESPN field {field!r} must be an object")
    return value


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ESPNSchemaError(f"ESPN field {field!r} is required")
    return str(value).strip()


def _optional_string(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ESPNSchemaError(f"ESPN field {field!r} is not an ISO timestamp") from error
    else:
        raise ESPNSchemaError(f"ESPN field {field!r} is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(value, field)


def _parse_clock(value: Any) -> float | None:
    if value in (None, "", "--"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if ":" in text:
            parts = text.split(":")
            try:
                minutes = float(parts[0])
                seconds = float(parts[1])
            except (ValueError, IndexError):
                return None
            return minutes + seconds / 60
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _parse_int(value: Any, *, default: int = 0) -> int:
    if value in (None, "", "--"):
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if "-" in text and text.count("-") == 1:
            text = text.split("-", 1)[0]
        try:
            return int(float(text))
        except ValueError:
            return default
    return default


def _team_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return _optional_string(value.get("id"))
    return _optional_string(value)


def _status(value: Any) -> tuple[GameStatus, str | None]:
    status = _as_mapping(value, "status")
    status_type = status.get("type", status)
    if not isinstance(status_type, Mapping):
        status_type = {}
    tokens = " ".join(
        str(status_type.get(key, "")) for key in ("id", "name", "state", "description", "detail")
    ).casefold()
    if "postpon" in tokens:
        result = GameStatus.POSTPONED
    elif "cancel" in tokens:
        result = GameStatus.CANCELED
    elif "final" in tokens or status_type.get("completed") is True or "post" in tokens:
        result = GameStatus.FINAL
    elif "in_progress" in tokens or "in progress" in tokens or "live" in tokens:
        result = GameStatus.IN_PROGRESS
    elif "pre" in tokens or "scheduled" in tokens or "upcoming" in tokens:
        result = GameStatus.SCHEDULED
    else:
        result = GameStatus.UNKNOWN
    detail = _optional_string(
        status_type.get("detail")
        or status_type.get("description")
        or status_type.get("shortDetail")
    )
    return result, detail


def _validate[ModelT: BaseModel](
    model: type[ModelT], payload: Mapping[str, Any], name: str
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise ESPNSchemaError(f"Invalid ESPN {name} payload: {error}") from error


def _source(
    provider_id: str,
    retrieved_at: datetime,
    source_updated_at: datetime | None = None,
) -> SourceMetadata:
    return SourceMetadata(
        provider="espn",
        provider_id=provider_id,
        retrieved_at=retrieved_at,
        source_updated_at=source_updated_at,
    )


def _quality(
    resource: str,
    records: tuple[object, ...],
    retrieved_at: datetime,
    warnings: tuple[str, ...] = (),
) -> DataQualityReport:
    return quality_for_records(
        resource=resource,
        records=records,
        retrieved_at=retrieved_at,
        expires_at=retrieved_at + _RESOURCE_TTLS[resource],
        warnings=warnings,
    )


def _competition(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    competitions = payload.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        raise ESPNSchemaError("ESPN event is missing competitions")
    return _as_mapping(competitions[0], "competitions[0]")


def _parse_game(payload: Mapping[str, Any], retrieved_at: datetime) -> ScheduledGame:
    game_id = _required_string(payload.get("id"), "id")
    competition = _competition(payload)
    competitors = competition.get("competitors")
    if not isinstance(competitors, list):
        raise ESPNSchemaError(f"ESPN game {game_id!r} is missing competitors")
    home_team_id: str | None = None
    away_team_id: str | None = None
    for competitor in competitors:
        competitor_mapping = _as_mapping(competitor, "competitor")
        team_id = _team_id(competitor_mapping.get("team")) or _team_id(competitor_mapping.get("id"))
        if team_id is None:
            raise ESPNSchemaError(f"ESPN game {game_id!r} has a competitor without a team ID")
        if competitor_mapping.get("homeAway") == "home":
            home_team_id = team_id
        elif competitor_mapping.get("homeAway") == "away":
            away_team_id = team_id
    if home_team_id is None or away_team_id is None:
        raise ESPNSchemaError(f"ESPN game {game_id!r} does not identify home and away teams")
    timestamp = payload.get("date") or competition.get("date")
    start_time = _parse_datetime(timestamp, f"game {game_id}.date")
    game_status, status_detail = _status(competition.get("status", payload.get("status", {})))
    return ScheduledGame(
        provider_id=game_id,
        start_time=start_time,
        status=game_status,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        status_detail=status_detail,
        source=_source(game_id, retrieved_at),
    )


def parse_scoreboard(
    payload: Mapping[str, Any], *, retrieved_at: datetime
) -> ProviderResult[tuple[ScheduledGame, ...]]:
    validated = _validate(ESPNScoreboardPayload, payload, "scoreboard")
    games = tuple(
        _parse_game(_as_mapping(event, "event"), retrieved_at) for event in validated.events
    )
    return ProviderResult(games, _quality("scoreboard", games, retrieved_at))


def _summary_game(payload: Mapping[str, Any], retrieved_at: datetime) -> ScheduledGame:
    game_id = _required_string(payload.get("id"), "header.id")
    competition_values = payload.get("competitions")
    if not isinstance(competition_values, list) or not competition_values:
        raise ESPNSchemaError(f"ESPN game {game_id!r} summary is missing competitions")
    competition = _as_mapping(competition_values[0], "header.competitions[0]")
    event = {
        "id": game_id,
        "date": payload.get("date") or competition.get("date"),
        "competitions": [competition],
    }
    return _parse_game(event, retrieved_at)


def _stat_value(labels: list[str], values: list[Any], names: set[str]) -> int:
    for index, label in enumerate(labels):
        if label.casefold() in names and index < len(values):
            return _parse_int(values[index])
    return 0


def _parse_box_scores(
    payload: Mapping[str, Any], game: ScheduledGame, retrieved_at: datetime
) -> tuple[PlayerBoxScore, ...]:
    boxscore = _as_mapping(payload.get("boxscore", {}), "boxscore")
    team_blocks = boxscore.get("players", [])
    if not isinstance(team_blocks, list):
        raise ESPNSchemaError(f"ESPN game {game.provider_id!r} boxscore players must be a list")
    result: list[PlayerBoxScore] = []
    for team_block in team_blocks:
        team_mapping = _as_mapping(team_block, "boxscore.players entry")
        team_id = _team_id(team_mapping.get("team"))
        if team_id is None:
            raise ESPNSchemaError(f"ESPN game {game.provider_id!r} boxscore team is missing an ID")
        statistics = team_mapping.get("statistics", [])
        if not isinstance(statistics, list):
            raise ESPNSchemaError(f"ESPN game {game.provider_id!r} statistics must be a list")
        for statistic_block in statistics:
            statistic = _as_mapping(statistic_block, "boxscore.statistics entry")
            labels = [str(value) for value in statistic.get("labels", [])]
            athletes = statistic.get("athletes", [])
            if not isinstance(athletes, list):
                raise ESPNSchemaError("ESPN boxscore athletes must be a list")
            for athlete_block in athletes:
                athlete_entry = _as_mapping(athlete_block, "boxscore.athlete entry")
                athlete = _as_mapping(athlete_entry.get("athlete"), "boxscore.athlete")
                player_id = _required_string(athlete.get("id"), "boxscore.athlete.id")
                values = athlete_entry.get("stats", [])
                if not isinstance(values, list):
                    raise ESPNSchemaError(f"ESPN player {player_id!r} stats must be a list")
                minutes = _parse_clock(_stat_value_as_raw(labels, values, {"min", "minutes"}))
                did_not_play = bool(athlete_entry.get("didNotPlay", False))
                line = BoxScoreLine(
                    points=_stat_value(labels, values, {"pts", "points"}),
                    rebounds=_stat_value(labels, values, {"reb", "rebounds"}),
                    assists=_stat_value(labels, values, {"ast", "assists"}),
                    steals=_stat_value(labels, values, {"stl", "steals"}),
                    blocks=_stat_value(labels, values, {"blk", "blocks"}),
                    turnovers=_stat_value(labels, values, {"to", "turnovers"}),
                    three_pointers_made=_stat_value(labels, values, {"3pt", "3pm", "3p"}),
                    technical_fouls=_stat_value(labels, values, {"tf", "technical fouls"}),
                    flagrant_fouls=_stat_value(labels, values, {"ff", "flagrant fouls"}),
                )
                result.append(
                    PlayerBoxScore(
                        game_id=game.provider_id,
                        player_id=player_id,
                        team_id=team_id,
                        played_at=game.start_time,
                        started=bool(athlete_entry.get("starter", False)),
                        did_play=not did_not_play,
                        minutes=None if did_not_play else minutes,
                        line=line,
                        source=_source(f"{game.provider_id}:{player_id}", retrieved_at),
                    )
                )
    return tuple(result)


def _stat_value_as_raw(labels: list[str], values: list[Any], names: set[str]) -> Any:
    for index, label in enumerate(labels):
        if label.casefold() in names and index < len(values):
            return values[index]
    return None


def parse_game_summary(
    payload: Mapping[str, Any], *, retrieved_at: datetime
) -> ProviderResult[GameSummary]:
    validated = _validate(ESPNGameSummaryPayload, payload, "game summary")
    game = _summary_game(validated.header, retrieved_at)
    box_scores = _parse_box_scores(payload, game, retrieved_at)
    quality = _quality("game_summary", box_scores, retrieved_at)
    return ProviderResult(GameSummary(game=game, player_box_scores=box_scores), quality)


def _availability_status(value: Any) -> AvailabilityStatus:
    text = str(value or "").casefold()
    if "out" in text or "inactive" in text:
        return AvailabilityStatus.OUT
    if "doubtful" in text:
        return AvailabilityStatus.DOUBTFUL
    if "questionable" in text or "day-to-day" in text:
        return AvailabilityStatus.QUESTIONABLE
    if "probable" in text:
        return AvailabilityStatus.PROBABLE
    if "available" in text or "active" in text:
        return AvailabilityStatus.AVAILABLE
    return AvailabilityStatus.UNKNOWN


def parse_injuries(
    payload: Mapping[str, Any], *, retrieved_at: datetime
) -> ProviderResult[tuple[PlayerAvailability, ...]]:
    validated = _validate(ESPNInjuriesPayload, payload, "injuries")
    injury_entries: list[Mapping[str, Any]] = []
    for entry in validated.injuries:
        nested = entry.get("injuries")
        if isinstance(nested, list):
            injury_entries.extend(item for item in nested if isinstance(item, Mapping))
        else:
            injury_entries.append(entry)
    result: list[PlayerAvailability] = []
    for injury in injury_entries:
        athlete = injury.get("athlete", injury)
        athlete_mapping = _as_mapping(athlete, "injury.athlete")
        player_id = _required_string(athlete_mapping.get("id"), "injury.athlete.id")
        details = injury.get("details")
        detail_mapping = details if isinstance(details, Mapping) else {}
        detail = _optional_string(
            injury.get("shortComment")
            or injury.get("comment")
            or detail_mapping.get("detail")
            or detail_mapping.get("type")
        )
        source_updated_at = _parse_optional_datetime(
            injury.get("date") or injury.get("lastUpdated"), "injury.date"
        )
        result.append(
            PlayerAvailability(
                player_id=player_id,
                status=_availability_status(injury.get("status")),
                detail=detail,
                source=_source(player_id, retrieved_at, source_updated_at),
            )
        )
    records = tuple(result)
    return ProviderResult(records, _quality("injuries", records, retrieved_at))


def parse_team_roster(
    payload: Mapping[str, Any], *, team_id: str, retrieved_at: datetime
) -> ProviderResult[tuple[ProviderPlayer, ...]]:
    validated = _validate(ESPNTeamRosterPayload, payload, "team roster")
    roster: list[ProviderPlayer] = []
    payload_team = _team_id(payload.get("team")) or team_id
    payload_team_value = payload.get("team")
    payload_team_mapping: Mapping[str, Any] = (
        payload_team_value if isinstance(payload_team_value, Mapping) else {}
    )
    for athlete in validated.athletes:
        player_id = _required_string(athlete.get("id"), "roster.athlete.id")
        name = _optional_string(athlete.get("fullName") or athlete.get("displayName"))
        if name is None:
            raise ESPNSchemaError(f"ESPN roster player {player_id!r} has no name")
        athlete_team = athlete.get("team")
        athlete_team_mapping = athlete_team if isinstance(athlete_team, Mapping) else {}
        roster_team_id = _team_id(athlete_team_mapping) or payload_team
        roster.append(
            ProviderPlayer(
                provider_id=player_id,
                full_name=name,
                team_id=roster_team_id,
                team_abbreviation=_optional_string(
                    athlete_team_mapping.get("abbreviation")
                    or athlete_team_mapping.get("shortDisplayName")
                    or payload_team_mapping.get("abbreviation")
                ),
                active=athlete.get("active") if isinstance(athlete.get("active"), bool) else None,
                source=_source(player_id, retrieved_at),
            )
        )
    records = tuple(roster)
    return ProviderResult(records, _quality("team_roster", records, retrieved_at))


def parse_team_schedule(
    payload: Mapping[str, Any], *, retrieved_at: datetime
) -> ProviderResult[tuple[ScheduledGame, ...]]:
    validated = _validate(ESPNTeamSchedulePayload, payload, "team schedule")
    games = tuple(
        _parse_game(_as_mapping(event, "schedule.event"), retrieved_at)
        for event in validated.events
    )
    return ProviderResult(games, _quality("team_schedule", games, retrieved_at))


class ESPNClient:
    """Replaceable adapter around ESPN's public NBA JSON endpoints."""

    def __init__(
        self,
        *,
        base_url: str = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
        timeout: timedelta = timedelta(seconds=15),
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout.total_seconds(),
        )
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __aenter__(self) -> "ESPNClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, **params: str | int) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ESPNAPIError(f"ESPN returned {response.status_code} for {path}") from error
        except httpx.HTTPError as error:
            raise ESPNAPIError(f"ESPN request failed for {path}") from error
        payload = response.json()
        if not isinstance(payload, dict):
            raise ESPNAPIError(f"ESPN returned an unexpected payload for {path}")
        return payload

    async def scoreboard(self, game_date: date) -> ProviderResult[tuple[ScheduledGame, ...]]:
        payload = await self._get("/scoreboard", dates=game_date.strftime("%Y%m%d"), limit=100)
        return parse_scoreboard(payload, retrieved_at=self._clock())

    async def game_summary(self, game_id: str) -> ProviderResult[GameSummary]:
        payload = await self._get("/summary", event=game_id)
        return parse_game_summary(payload, retrieved_at=self._clock())

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]:
        payload = await self._get("/injuries")
        return parse_injuries(payload, retrieved_at=self._clock())

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        payload = await self._get(f"/teams/{team_id}/roster")
        return parse_team_roster(payload, team_id=team_id, retrieved_at=self._clock())

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        payload = await self._get(f"/teams/{team_id}/schedule", season=season)
        return parse_team_schedule(payload, retrieved_at=self._clock())
