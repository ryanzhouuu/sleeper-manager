"""Cache this week's opponent game projections once per manager-local day.

Only the daily and pre-tipoff runs refresh it, and only when the day has no row yet, so
postgame passes never pay for the opponent. A player whose identity did not resolve is
stored as unknown rather than as a player without games.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.cloudflare.home_cache import read_home_cache, write_home_cache
from sleeper_manager.cloudflare.planning import CloudflarePlanningAssembly
from sleeper_manager.cloudflare.stage_logging import timed_stage
from sleeper_manager.decisions.win_probability import game_outcomes
from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence


@dataclass(frozen=True, slots=True)
class OpponentGame:
    """``outcomes`` is None when the game had started before the projection ran."""

    start: datetime
    outcomes: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class OpponentOutlook:
    """``players`` maps each opponent roster player to their week games, or None if unknown."""

    roster_id: int
    projected_at: datetime
    players: Mapping[str, tuple[OpponentGame, ...] | None]


def outlook_cache_key(league_id: str, week: int) -> str:
    return f"opponent-outlook:{league_id}:{week}"


def outlook_payload(evidence: CollectedLiveEvidence) -> dict[str, object] | None:
    """Encode an opponent's collected evidence; None when a team schedule failed to load."""
    inputs = evidence.inputs
    if any(result.quality.state is DataQualityState.ERROR for result in inputs.schedule_results):
        return None
    projected_at = evidence.decision_time
    snapshots = {(row.sleeper_player_id, row.game_id): row.snapshot for row in inputs.projections}
    targets: dict[str, list[LiveProjectionTarget]] = {}
    for target in sorted(evidence.projection_targets, key=lambda item: item.game_start):
        targets.setdefault(target.sleeper_player_id, []).append(target)
    players: dict[str, list[dict[str, object]] | None] = {}
    for identity in inputs.identities:
        player_id = identity.sleeper_player_id
        if identity.provider_player_id is None or identity.provider_team_id is None:
            players[player_id] = None
            continue
        players[player_id] = _player_games(targets.get(player_id, ()), snapshots, projected_at)
    return {
        "roster_id": inputs.league_profile.manager_roster_id,
        "projected_at": projected_at.isoformat(),
        "players": players,
    }


def _player_games(
    targets: Sequence[LiveProjectionTarget],
    snapshots: Mapping[tuple[str, str], ProjectionSnapshot | None],
    projected_at: datetime,
) -> list[dict[str, object]] | None:
    """None when an unstarted game has no projection."""
    games: list[dict[str, object]] = []
    for target in targets:
        snapshot = snapshots.get((target.sleeper_player_id, target.game_id))
        if target.game_start <= projected_at:
            outcomes = None
        elif snapshot is None:
            return None
        else:
            outcomes = list(game_outcomes(snapshot.distribution))
        games.append({"start": target.game_start.isoformat(), "outcomes": outcomes})
    return games


def decode_outlook(payload: object) -> OpponentOutlook | None:
    """None when the cached row is not an outlook this module wrote."""
    if not isinstance(payload, dict):
        return None
    roster_id, projected_at, raw_players = (
        payload.get("roster_id"),
        payload.get("projected_at"),
        payload.get("players"),
    )
    if not isinstance(roster_id, int) or not isinstance(projected_at, str):
        return None
    if not isinstance(raw_players, dict):
        return None
    try:
        players = {
            player_id: None if games is None else tuple(_game(game) for game in games)
            for player_id, games in raw_players.items()
        }
        return OpponentOutlook(roster_id, datetime.fromisoformat(projected_at), players)
    except (TypeError, ValueError):
        return None


async def load_opponent_outlook(
    repository: AsyncRuntimeStateRepository, league_id: str, week: int, *, now: datetime
) -> OpponentOutlook | None:
    cached = await read_home_cache(repository, outlook_cache_key(league_id, week), now)
    return decode_outlook(cached)


async def refresh_opponent_outlook(
    repository: AsyncRuntimeStateRepository,
    assembly: CloudflarePlanningAssembly,
    *,
    now: datetime,
) -> None:
    """Project the opponent unless today already has an outlook. Failures only log."""
    profile = assembly.evidence.inputs.league_profile
    key = outlook_cache_key(profile.league_id, profile.fantasy_week.week)
    if assembly.collect_opponent is None or await read_home_cache(repository, key, now):
        return
    try:
        with timed_stage("opponent_outlook"):
            evidence = await assembly.collect_opponent()
            payload = None if evidence is None else outlook_payload(evidence)
    except Exception as error:
        print(f"Opponent outlook failed: {type(error).__name__}", file=sys.stderr)
        return
    if payload is not None:
        await write_home_cache(
            repository,
            key,
            payload,
            resource="opponent-outlook",
            timezone_name=assembly.local_timezone.key,
            now=now,
        )


def _game(raw: object) -> OpponentGame:
    if not isinstance(raw, dict) or not isinstance(raw.get("start"), str):
        raise ValueError("Cached opponent game is malformed")
    outcomes = raw.get("outcomes")
    if outcomes is not None and (
        not isinstance(outcomes, list)
        or not outcomes
        or not all(isinstance(value, int | float) for value in outcomes)
    ):
        raise ValueError("Cached opponent outcomes are malformed")
    start = datetime.fromisoformat(raw["start"])
    if start.tzinfo is None:
        raise ValueError("Cached opponent game start must be timezone-aware")
    return OpponentGame(start, None if outcomes is None else tuple(float(v) for v in outcomes))


__all__ = (
    "OpponentGame",
    "OpponentOutlook",
    "decode_outlook",
    "load_opponent_outlook",
    "outlook_cache_key",
    "outlook_payload",
    "refresh_opponent_outlook",
)
