"""Read the per-game stat rows behind the scores shown in the Sleeper app."""

from collections.abc import Mapping
from datetime import date
from typing import Any

from sleeper_manager.domain.scoring import BoxScoreLine


class SleeperGameStatsError(ValueError):
    """Raised when a Sleeper stat payload cannot be trusted for scoring."""


def player_game_stats(payload: Mapping[str, Any], game_date: date) -> Mapping[str, Any] | None:
    """Return one game's stats, or None without a row. Sleeper's dates are US Eastern.

    Sleeper also publishes empty stats for a game the player missed or that has not posted.
    """

    return _single_game(payload, "date", game_date.isoformat())


def game_stats_by_id(payload: Mapping[str, Any], game_id: str) -> Mapping[str, Any] | None:
    """Return the stats for the Sleeper game id a lock names, or None without a row."""

    return _single_game(payload, "game_id", game_id)


def game_date_by_id(payload: Mapping[str, Any], game_id: str) -> date | None:
    """Return the US Eastern date Sleeper lists for a game id, or None without a row."""

    row = _single_row(payload, "game_id", game_id)
    if row is None:
        return None
    try:
        return date.fromisoformat(str(row.get("date")))
    except ValueError as error:
        raise SleeperGameStatsError(f"Sleeper game {game_id} has no valid date") from error


def _single_game(payload: Mapping[str, Any], key: str, value: str) -> Mapping[str, Any] | None:
    row = _single_row(payload, key, value)
    if row is None:
        return None
    stats = row.get("stats") or {}
    if not isinstance(stats, Mapping):
        raise SleeperGameStatsError("Sleeper stats must be an object")
    return stats


def _single_row(payload: Mapping[str, Any], key: str, value: str) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    for rows in payload.values():
        if not isinstance(rows, list):
            raise SleeperGameStatsError("Sleeper weekly stat rows must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise SleeperGameStatsError("Sleeper stat row must be an object")
            if row.get(key) == value:
                matches.append(row)
    if not matches:
        return None
    if len(matches) > 1:
        raise SleeperGameStatsError(f"Sleeper has {len(matches)} stat rows for {key} {value}")
    return matches[0]


def box_score_line(stats: Mapping[str, Any]) -> BoxScoreLine:
    """Convert Sleeper's omitted-when-zero counts to the shared scoring line."""

    return BoxScoreLine(
        points=_count(stats, "pts"),
        rebounds=_count(stats, "reb"),
        assists=_count(stats, "ast"),
        steals=_count(stats, "stl"),
        blocks=_count(stats, "blk"),
        turnovers=_count(stats, "to"),
        three_pointers_made=_count(stats, "tpm"),
        technical_fouls=_count(stats, "tf"),
        flagrant_fouls=_count(stats, "ff"),
    )


def _count(stats: Mapping[str, Any], key: str) -> int:
    value = stats.get(key, 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or value < 0
        or value != int(value)
    ):
        raise SleeperGameStatsError(f"Sleeper stat {key!r} is not a count: {value!r}")
    return int(value)
