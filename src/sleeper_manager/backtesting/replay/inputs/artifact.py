"""Load persisted team-week artifacts and validate their bundle relationships.

JSON-to-domain decoding lives in `_artifact_decoding`; this module owns file
I/O, canonical bundle layout checks, and cross-record identity validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.replay.inputs._artifact_decoding import (
    HistoricalTeamWeekArtifactError,
    decode_historical_team_week,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput


def load_historical_team_week_artifact(path: Path) -> HistoricalTeamWeekInput:
    """Reconstruct one HistoricalTeamWeekInput from a persisted JSON artifact."""

    payload = _read_object(path)
    team_week = decode_historical_team_week(payload)
    _validate_team_week_relationships(team_week)
    _validate_bundle_layout(path, team_week)
    return team_week


def _read_object(path: Path) -> dict[str, Any]:
    """Read one JSON object and translate file or syntax failures."""

    try:
        raw = path.read_text()
    except OSError as error:
        raise HistoricalTeamWeekArtifactError(
            f"Unable to read historical team-week artifact: {path}"
        ) from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HistoricalTeamWeekArtifactError(
            f"Historical team-week artifact is not valid JSON: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise HistoricalTeamWeekArtifactError(
            "Historical team-week artifact root must be a JSON object"
        )
    return payload


def _validate_bundle_layout(path: Path, team_week: HistoricalTeamWeekInput) -> None:
    """Validate canonical path identities and the enclosing manifest."""

    parts = path.parts
    try:
        team_weeks_index = parts.index("team-weeks")
    except ValueError:
        return
    trailing = parts[team_weeks_index:]
    if len(trailing) != 4:
        raise HistoricalTeamWeekArtifactError(
            "Canonical team-week path must be team-weeks/<league>/week-<n>/roster-<id>.json"
        )
    _, league_id, week_token, roster_file = trailing
    if league_id != team_week.league_id:
        raise HistoricalTeamWeekArtifactError(
            "Team-week path league identity does not match the payload"
        )
    if week_token != f"week-{team_week.week:02d}":
        raise HistoricalTeamWeekArtifactError(
            "Team-week path week identity does not match the payload"
        )
    if Path(roster_file).stem != f"roster-{team_week.roster_id}":
        raise HistoricalTeamWeekArtifactError(
            "Team-week path roster identity does not match the payload"
        )
    bundle_root = path.parents[3]
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        raise HistoricalTeamWeekArtifactError(
            f"Canonical team-week bundle is missing manifest.json beside {path}"
        )
    manifest = _read_object(manifest_path)
    manifest_id = _manifest_id(manifest)
    if manifest_id != team_week.manifest_id:
        raise HistoricalTeamWeekArtifactError(
            "Team-week payload manifest_id disagrees with the enclosing manifest"
        )


def _manifest_id(payload: dict[str, Any]) -> str:
    """Require the manifest's non-empty content identity."""

    value = payload.get("manifest_id")
    if not isinstance(value, str) or not value.strip():
        raise HistoricalTeamWeekArtifactError("manifest_id must be a non-empty string")
    return value


def _validate_team_week_relationships(team_week: HistoricalTeamWeekInput) -> None:
    """Reject nested evidence that disagrees with its enclosing team-week identities."""

    game_ids = tuple(game.game_id for game in team_week.games)
    if len(set(game_ids)) != len(game_ids):
        raise HistoricalTeamWeekArtifactError("Replay game IDs must be unique")
    if any(game.week != team_week.week for game in team_week.games):
        raise HistoricalTeamWeekArtifactError("Replay game week disagrees with the team-week")

    roster_players = set(team_week.roster_player_ids)
    if len(roster_players) != len(team_week.roster_player_ids):
        raise HistoricalTeamWeekArtifactError("Roster player IDs must be unique")
    observed_starters = tuple(
        player_id for player_id in team_week.observed_starter_ids if player_id is not None
    )
    if len(set(observed_starters)) != len(observed_starters):
        raise HistoricalTeamWeekArtifactError("Observed starter IDs must be unique")
    if not set(observed_starters) <= roster_players:
        raise HistoricalTeamWeekArtifactError("Observed starters must belong to the roster")

    player_game_keys = tuple(
        (player_game.sleeper_id, player_game.game_id) for player_game in team_week.player_games
    )
    if len(set(player_game_keys)) != len(player_game_keys):
        raise HistoricalTeamWeekArtifactError("Replay player-game identities must be unique")
    known_games = set(game_ids)
    for player_game in team_week.player_games:
        if player_game.game_id not in known_games:
            raise HistoricalTeamWeekArtifactError(
                f"Replay player-game references unknown game {player_game.game_id!r}"
            )
        if player_game.fantasy_team_id != team_week.roster_id:
            raise HistoricalTeamWeekArtifactError(
                "Replay player-game roster disagrees with the team-week"
            )
        if player_game.sleeper_id not in roster_players:
            raise HistoricalTeamWeekArtifactError(
                "Replay player-game player does not belong to the roster"
            )
        if not player_game.eligible_positions:
            raise HistoricalTeamWeekArtifactError(
                "Replay player-games require at least one eligible position"
            )
        projection = player_game.projection
        if projection is not None and (
            projection.player_id != player_game.sleeper_id
            or projection.game_id != player_game.game_id
        ):
            raise HistoricalTeamWeekArtifactError(
                "Replay player-game projection identity disagrees with its parent"
            )


__all__ = (
    "HistoricalTeamWeekArtifactError",
    "load_historical_team_week_artifact",
)
