"""Local-day cache for the home document's player names and team schedules.

A failed live read does not write a row. A cache read or write that raises is
treated as a miss so the page can still use the live response.
"""

import json
from datetime import datetime
from typing import cast

from sleeper_manager.cloudflare.home_document import HomeIdentity
from sleeper_manager.domain.nba import DataQualityState, GameStatus, ScheduledGame, SourceMetadata
from sleeper_manager.domain.scheduling import manager_local_day_window
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, CachedNBARecord

_CACHE_PROVIDER = "home"
_CACHE_SCHEMA = "1"


async def read_home_cache(
    repository: AsyncRuntimeStateRepository,
    key: str,
    now: datetime,
) -> object | None:
    """Return a fresh JSON value, or None when the row is missing, stale, or unreadable."""
    try:
        record = await repository.get(key, now=now)
    except Exception:
        return None
    if record is None or record.quality is not DataQualityState.FRESH:
        return None
    try:
        return cast(object, json.loads(record.payload_json))
    except json.JSONDecodeError:
        return None


async def write_home_cache(
    repository: AsyncRuntimeStateRepository,
    key: str,
    payload: object,
    *,
    resource: str,
    timezone_name: str,
    now: datetime,
) -> None:
    """Store a fresh row until the manager-local day ends. Write errors are ignored."""
    try:
        window = manager_local_day_window(now, timezone_name=timezone_name)
        await repository.put(
            CachedNBARecord(
                cache_key=key,
                provider=_CACHE_PROVIDER,
                resource=resource,
                schema_version=_CACHE_SCHEMA,
                payload_json=json.dumps(payload, separators=(",", ":")),
                retrieved_at=now,
                source_updated_at=None,
                expires_at=window.ends_at,
                quality=DataQualityState.FRESH,
            )
        )
    except Exception:
        return


def decode_identities(payload: object | None) -> dict[str, HomeIdentity] | None:
    """Decode a cached name map. None when the payload is not that map.

    Entries written before positions were cached have no `positions` key and
    count as a miss, so the catalog is read again.
    """
    if not isinstance(payload, dict):
        return None
    identities: dict[str, HomeIdentity] = {}
    for sleeper_id, raw in payload.items():
        if not isinstance(sleeper_id, str) or not isinstance(raw, dict):
            return None
        name = raw.get("name")
        nba_team = raw.get("nba_team")
        espn_id = raw.get("espn_id")
        if name is not None and not isinstance(name, str):
            return None
        if nba_team is not None and not isinstance(nba_team, str):
            return None
        if espn_id is not None and not isinstance(espn_id, str):
            return None
        positions = raw.get("positions")
        if not isinstance(positions, list) or not all(isinstance(item, str) for item in positions):
            return None
        injury_status = raw.get("injury_status")
        if injury_status is not None and not isinstance(injury_status, str):
            return None
        identities[sleeper_id] = HomeIdentity(
            sleeper_id, name, nba_team, espn_id, tuple(positions), injury_status
        )
    return identities


def decode_schedule(payload: object | None, now: datetime) -> tuple[ScheduledGame, ...] | None:
    """Decode cached schedule rows. None when any row is unusable."""
    if not isinstance(payload, list):
        return None
    games: list[ScheduledGame] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        try:
            start = datetime.fromisoformat(str(item["start"]))
            status = GameStatus(str(item["status"]))
        except (KeyError, TypeError, ValueError):
            return None
        if start.tzinfo is None:
            return None
        home = item.get("home")
        away = item.get("away")
        if home is not None and not isinstance(home, str):
            return None
        if away is not None and not isinstance(away, str):
            return None
        games.append(
            ScheduledGame(
                provider_id="cached",
                start_time=start,
                status=status,
                home_team_id="home",
                away_team_id="away",
                status_detail=None,
                source=SourceMetadata(
                    provider=_CACHE_PROVIDER,
                    provider_id="cached",
                    retrieved_at=now,
                ),
                home_abbreviation=home,
                away_abbreviation=away,
            )
        )
    return tuple(games)
