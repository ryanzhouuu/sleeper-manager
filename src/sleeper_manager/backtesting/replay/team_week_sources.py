"""Load and fingerprint the raw evidence for one historical team-week.

This module owns the Sleeper cache boundary and SportsDataverse season lookup.
The public bundle command orchestrates these helpers; projection construction
lives in ``team_week_projection``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs
from sleeper_manager.backtesting.replay.inputs import SourceFingerprint, source_fingerprint
from sleeper_manager.backtesting.replay.league_archive import (
    HistoricalLeagueArchive,
    acquire_sleeper_archive,
    parse_historical_league_archive,
)
from sleeper_manager.integrations.sleeper.client import SleeperClient


class HistoricalTeamWeekBundleError(RuntimeError):
    """Raise when selected raw evidence cannot form one reproducible team-week."""


def ensure_sleeper_archive(
    workspace: Path,
    *,
    league_id: str,
    week: int,
    retrieved_at: datetime,
) -> bool:
    """Acquire only the selected Sleeper week when its required cache files are absent."""

    root = workspace / "sleeper" / league_id
    required = (
        root / "league.json",
        root / "rosters.json",
        root / "players.json",
        root / "weeks" / f"{week:02d}" / "matchups.json",
        root / "weeks" / f"{week:02d}" / "transactions.json",
    )
    if all(path.is_file() for path in required):
        return False
    asyncio.run(_acquire_selected_week(workspace, league_id, week, retrieved_at))
    if not all(path.is_file() for path in required):
        raise HistoricalTeamWeekBundleError(
            "Sleeper archive acquisition did not produce the required files"
        )
    return True


async def _acquire_selected_week(
    workspace: Path,
    league_id: str,
    week: int,
    retrieved_at: datetime,
) -> None:
    """Capture the raw Sleeper payloads that can affect one selected week."""

    async with SleeperClient() as reader:
        await acquire_sleeper_archive(
            reader,
            league_id=league_id,
            root=workspace / "sleeper",
            weeks=(week,),
            retrieved_at=retrieved_at,
        )


def load_selected_archive(
    workspace: Path,
    *,
    league_id: str,
    roster_id: int,
    week: int,
    fallback_retrieved_at: datetime,
) -> HistoricalLeagueArchive:
    """Parse only selected-week cache records, excluding unrelated cached weeks."""

    root = workspace / "sleeper" / league_id
    week_root = root / "weeks" / f"{week:02d}"
    raw_matchups = _read_payload(week_root / "matchups.json", required=False)
    raw_transactions = _read_payload(week_root / "transactions.json", required=False)
    matchup_weeks = {
        week: [item for item in raw_matchups if isinstance(item, Mapping)]
        if isinstance(raw_matchups, list)
        else []
    }
    transactions = (
        [item for item in raw_transactions if isinstance(item, Mapping)]
        if isinstance(raw_transactions, list)
        else []
    )
    catalog = player_catalog(workspace, league_id)
    return parse_historical_league_archive(
        _required_mapping(_read_payload(root / "league.json"), "league"),
        rosters=_mapping_records(_read_payload(root / "rosters.json"), "rosters"),
        matchup_weeks=matchup_weeks,
        transactions=transactions,
        player_catalog={
            sleeper_id: catalog[sleeper_id]
            for sleeper_id in _target_player_ids(roster_id, week, matchup_weeks, transactions)
            if sleeper_id in catalog
        },
        retrieved_at=_archive_retrieved_at(root, fallback_retrieved_at),
    )


def player_catalog(workspace: Path, league_id: str) -> dict[str, Mapping[str, Any]]:
    """Read well-formed catalog records keyed by Sleeper player ID."""

    return _mapping_catalog(_read_payload(workspace / "sleeper" / league_id / "players.json"))


def nba_season_from_archive(archive: HistoricalLeagueArchive) -> int:
    """Convert Sleeper's NBA season start year into SportsDataverse's ending-year directory."""

    try:
        return int(archive.season) + 1
    except ValueError as error:
        raise HistoricalTeamWeekBundleError(
            f"Sleeper season {archive.season!r} cannot select an NBA data directory"
        ) from error


def source_fingerprints(
    workspace: Path,
    *,
    league_id: str,
    week: int,
    nba_inputs: HistoricalExperimentInputs,
    finalization_policy_version: str,
) -> tuple[SourceFingerprint, ...]:
    """Fingerprint all selected raw files and the explicit approximate finalization policy."""

    root = workspace / "sleeper" / league_id
    sleeper_paths = (
        ("sleeper:league", root / "league.json"),
        ("sleeper:rosters", root / "rosters.json"),
        ("sleeper:players", root / "players.json"),
        ("sleeper:matchups", root / "weeks" / f"{week:02d}" / "matchups.json"),
        ("sleeper:transactions", root / "weeks" / f"{week:02d}" / "transactions.json"),
    )
    sleeper = tuple(source_fingerprint(name, path.read_bytes()) for name, path in sleeper_paths)
    nba = tuple(
        SourceFingerprint(
            f"sportsdataverse:{artifact.season}:{artifact.resource}",
            artifact.sha256,
            "rds-v1",
        )
        for artifact in nba_inputs.artifacts
    )
    finalization_bound = source_fingerprint(
        "approximate-finalization-bound",
        finalization_policy_version.encode(),
        version=finalization_policy_version,
    )
    return (*sleeper, *nba, finalization_bound)


def _target_player_ids(
    roster_id: int,
    week: int,
    matchup_weeks: Mapping[int, Sequence[Mapping[str, Any]]],
    transactions: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Collect catalog IDs whose position evidence can affect the selected roster-week."""

    player_ids: set[str] = set()
    for matchup in matchup_weeks.get(week, ()):
        if _roster_id(matchup.get("roster_id")) != roster_id:
            continue
        values = matchup.get("players")
        if isinstance(values, list):
            player_ids.update(str(value) for value in values if str(value).strip())
    for transaction in transactions:
        for key in ("adds", "drops"):
            values = transaction.get(key)
            if not isinstance(values, Mapping):
                continue
            player_ids.update(
                str(player_id)
                for player_id, transaction_roster_id in values.items()
                if _roster_id(transaction_roster_id) == roster_id
            )
    return tuple(sorted(player_ids))


def _roster_id(value: object) -> int | None:
    """Normalize raw Sleeper roster IDs without treating malformed values as matches."""

    if not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_payload(path: Path, *, required: bool = True) -> object:
    """Load one raw JSON file, reporting unavailable or invalid selected evidence."""

    if not path.is_file():
        if required:
            raise HistoricalTeamWeekBundleError(f"Missing cached Sleeper artifact: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalTeamWeekBundleError(
            f"Could not read cached Sleeper artifact: {path}"
        ) from error


def _required_mapping(payload: object, label: str) -> Mapping[str, Any]:
    """Require an object-shaped raw payload at a named external boundary."""

    if not isinstance(payload, Mapping):
        raise HistoricalTeamWeekBundleError(f"Cached Sleeper {label} payload must be an object")
    return payload


def _mapping_records(payload: object, label: str) -> tuple[Mapping[str, Any], ...]:
    """Require an object-list raw payload at a named external boundary."""

    if not isinstance(payload, list):
        raise HistoricalTeamWeekBundleError(f"Cached Sleeper {label} payload must be a list")
    return tuple(item for item in payload if isinstance(item, Mapping))


def _mapping_catalog(payload: object) -> dict[str, Mapping[str, Any]]:
    """Discard malformed catalog entries before selected-player identity parsing."""

    if not isinstance(payload, Mapping):
        raise HistoricalTeamWeekBundleError("Cached Sleeper players payload must be an object")
    return {
        str(player_id): record
        for player_id, record in payload.items()
        if isinstance(record, Mapping)
    }


def _archive_retrieved_at(root: Path, fallback: datetime) -> datetime:
    """Use cached archive acquisition time when present, otherwise this bundle's retrieval time."""

    manifest = _read_payload(root / "manifest.json", required=False)
    if isinstance(manifest, Mapping) and isinstance(manifest.get("retrieved_at"), str):
        try:
            parsed = datetime.fromisoformat(manifest["retrieved_at"].replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    return fallback.astimezone(UTC)


__all__ = (
    "HistoricalTeamWeekBundleError",
    "ensure_sleeper_archive",
    "load_selected_archive",
    "nba_season_from_archive",
    "player_catalog",
    "source_fingerprints",
)
