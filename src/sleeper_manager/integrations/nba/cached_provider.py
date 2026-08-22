"""Cache-through NBA provider decorator over parsed provider results."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    DataQualityReport,
    DataQualityState,
    GameStatus,
    PlayerAvailability,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.persistence.base import CachedNBARecord, NBADataCache

RecordT = TypeVar("RecordT")


class CachedNBAProvider:
    """Serves team rosters, schedules, and injuries through an NBADataCache."""

    def __init__(
        self,
        inner: Any,
        cache: NBADataCache,
        *,
        clock: Callable[[], datetime] | None = None,
        provider_name: str = "nba",
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._clock = clock or (lambda: datetime.now(UTC))
        self._provider_name = provider_name

    async def scoreboard(self, game_date: date):  # type: ignore[no-untyped-def]
        raise NotImplementedError("Scoreboard caching is not part of the planning path")

    async def game_summary(self, game_id: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError("Game summaries are not cached by this wrapper")

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        return await self._through(
            f"team-roster:{team_id}",
            lambda: self._inner.team_roster(team_id),
            _encode_players,
            _decode_players,
        )

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        return await self._through(
            f"team-schedule:{team_id}:{season}",
            lambda: self._inner.team_schedule(team_id, season),
            _encode_games,
            _decode_games,
        )

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]:
        return await self._through(
            "injuries",
            lambda: self._inner.injuries(),
            _encode_availability,
            _decode_availability,
        )

    async def _through(
        self,
        resource_key: str,
        fetch: Callable[[], Awaitable[ProviderResult[tuple[RecordT, ...]]]],
        encode: Callable[[tuple[RecordT, ...]], list[dict[str, Any]]],
        decode: Callable[[list[dict[str, Any]]], tuple[RecordT, ...]],
    ) -> ProviderResult[tuple[RecordT, ...]]:
        now = self._clock()
        cache_key = f"{self._provider_name}:{resource_key}"
        cached = self._cache.get(cache_key, now=now)
        if cached is not None and cached.quality is not DataQualityState.STALE:
            return _result_from_record(cached, decode)
        result = await fetch()
        self._cache.put(
            CachedNBARecord(
                cache_key=cache_key,
                provider=self._provider_name,
                resource=resource_key.split(":", 1)[0],
                schema_version="1",
                payload_json=_dump_json(encode(result.records)),
                retrieved_at=result.quality.retrieved_at,
                source_updated_at=result.quality.source_updated_at,
                expires_at=result.quality.expires_at,
                quality=result.quality.state,
                warnings=result.quality.warnings,
                errors=result.quality.errors,
            )
        )
        return result


def _result_from_record[RecordT](
    record: CachedNBARecord,
    decode: Callable[[list[dict[str, Any]]], tuple[RecordT, ...]],
) -> ProviderResult[tuple[RecordT, ...]]:
    records = decode(_load_json(record.payload_json))
    quality = DataQualityReport(
        state=record.quality,
        resource=record.resource,
        record_count=len(records),
        retrieved_at=record.retrieved_at,
        source_updated_at=record.source_updated_at,
        expires_at=record.expires_at,
        warnings=record.warnings,
        errors=record.errors,
    )
    return ProviderResult(tuple(records), quality)


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _load_json(payload_json: str) -> Any:
    return json.loads(payload_json)


def _encode_source(source: SourceMetadata) -> dict[str, Any]:
    return {
        "provider": source.provider,
        "provider_id": source.provider_id,
        "retrieved_at": _iso(source.retrieved_at),
        "source_updated_at": _iso(source.source_updated_at),
        "schema_version": source.schema_version,
        "content_hash": source.content_hash,
    }


def _decode_source(payload: Mapping[str, Any]) -> SourceMetadata:
    retrieved_at = _parse(payload["retrieved_at"])
    if retrieved_at is None:
        raise ValueError("Cached source metadata is missing its retrieval time")
    return SourceMetadata(
        provider=payload["provider"],
        provider_id=payload["provider_id"],
        retrieved_at=retrieved_at,
        source_updated_at=_parse(payload["source_updated_at"]),
        schema_version=payload["schema_version"],
        content_hash=payload["content_hash"],
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _encode_players(records: tuple[ProviderPlayer, ...]) -> list[dict[str, Any]]:
    return [
        {
            "provider_id": item.provider_id,
            "full_name": item.full_name,
            "team_id": item.team_id,
            "team_abbreviation": item.team_abbreviation,
            "active": item.active,
            "source": _encode_source(item.source),
        }
        for item in records
    ]


def _decode_players(payload: list[dict[str, Any]]) -> tuple[ProviderPlayer, ...]:
    return tuple(
        ProviderPlayer(
            provider_id=item["provider_id"],
            full_name=item["full_name"],
            team_id=item["team_id"],
            team_abbreviation=item["team_abbreviation"],
            active=item["active"],
            source=_decode_source(item["source"]),
        )
        for item in payload
    )


def _encode_games(records: tuple[ScheduledGame, ...]) -> list[dict[str, Any]]:
    return [
        {
            "provider_id": item.provider_id,
            "start_time": _iso(item.start_time),
            "status": item.status.value,
            "home_team_id": item.home_team_id,
            "away_team_id": item.away_team_id,
            "status_detail": item.status_detail,
            "source": _encode_source(item.source),
            "venue_id": item.venue_id,
            "venue_name": item.venue_name,
            "venue_city": item.venue_city,
            "venue_state": item.venue_state,
            "neutral_site": item.neutral_site,
            "regulation_periods": item.regulation_periods,
            "completed_periods": item.completed_periods,
            "finalized_at": _iso(item.finalized_at),
        }
        for item in records
    ]


def _decode_games(payload: list[dict[str, Any]]) -> tuple[ScheduledGame, ...]:
    return tuple(
        ScheduledGame(
            provider_id=item["provider_id"],
            start_time=datetime.fromisoformat(item["start_time"]),
            status=GameStatus(item["status"]),
            home_team_id=item["home_team_id"],
            away_team_id=item["away_team_id"],
            status_detail=item["status_detail"],
            source=_decode_source(item["source"]),
            venue_id=item["venue_id"],
            venue_name=item["venue_name"],
            venue_city=item["venue_city"],
            venue_state=item["venue_state"],
            neutral_site=item["neutral_site"],
            regulation_periods=item["regulation_periods"],
            completed_periods=item["completed_periods"],
            finalized_at=_parse(item["finalized_at"]),
        )
        for item in payload
    )


def _encode_availability(records: tuple[PlayerAvailability, ...]) -> list[dict[str, Any]]:
    return [
        {
            "player_id": item.player_id,
            "status": item.status.value,
            "detail": item.detail,
            "source": _encode_source(item.source),
        }
        for item in records
    ]


def _decode_availability(payload: list[dict[str, Any]]) -> tuple[PlayerAvailability, ...]:
    return tuple(
        PlayerAvailability(
            player_id=item["player_id"],
            status=AvailabilityStatus(item["status"]),
            detail=item["detail"],
            source=_decode_source(item["source"]),
        )
        for item in payload
    )


__all__ = ("CachedNBAProvider",)
