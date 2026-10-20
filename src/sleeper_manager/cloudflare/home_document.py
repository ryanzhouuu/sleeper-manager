"""Public JSON for GET /home.

Callers pass already loaded league, schedule, and recommendation data.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.scheduling import TimeWindow
from sleeper_manager.integrations.nba.mapping import espn_team_endpoint_id, normalize_team

_KEPT_GAME_STATUSES = frozenset({GameStatus.SCHEDULED, GameStatus.IN_PROGRESS, GameStatus.FINAL})


@dataclass(frozen=True, slots=True)
class HomeIdentity:
    """Name and image ids for one Sleeper player."""

    sleeper_id: str
    name: str | None
    nba_team: str | None
    espn_id: str | None


@dataclass(frozen=True, slots=True)
class HomeSide:
    """One fantasy matchup participant."""

    name: str | None
    score: float | None
    image_url: str | None


@dataclass(frozen=True, slots=True)
class RosterRow:
    """One starter, bench, or reserve slot before image URLs are added."""

    slot: str
    name: str | None
    nba_team: str | None
    espn_id: str | None


@dataclass(frozen=True, slots=True)
class GameRow:
    """One player-game already tied to a roster player."""

    start: datetime
    player_name: str | None
    espn_id: str | None
    opponent: str | None
    home: bool


@dataclass(frozen=True, slots=True)
class LabeledGame:
    """A team game reduced to tipoff, opponent abbreviation, and home flag."""

    start: datetime
    opponent: str | None
    home: bool


def player_image_url(espn_id: str | None) -> str | None:
    """ESPN headshot for a non-empty id. Blank ids have no image."""
    if espn_id is None or not espn_id.strip():
        return None
    return f"https://a.espncdn.com/i/headshots/nba/players/full/{espn_id.strip()}.png"


def team_image_url(abbreviation: str | None) -> str | None:
    """ESPN logo slug from the existing team-route abbreviation map."""
    if abbreviation is None or not abbreviation.strip():
        return None
    slug = espn_team_endpoint_id(abbreviation.strip()).casefold()
    return f"https://a.espncdn.com/i/teamlogos/nba/500/{slug}.png"


def avatar_image_url(avatar: str | None) -> str | None:
    """Sleeper thumbnail for a non-empty avatar id."""
    if avatar is None or not avatar.strip():
        return None
    return f"https://sleepercdn.com/avatars/thumbs/{avatar.strip()}"


def label_team_games(
    queried_team: str,
    games: Sequence[ScheduledGame],
    window: TimeWindow,
) -> tuple[LabeledGame, ...] | None:
    """Label kept games inside the window for one queried NBA team.

    Postponed, canceled, and unknown games are dropped. Returns None when a
    kept in-window game has a missing abbreviation or both sides match the
    queried team. Returns an empty tuple when nothing remains in the window.
    """
    queried = normalize_team(queried_team)
    labeled: list[LabeledGame] = []
    for game in games:
        if game.status not in _KEPT_GAME_STATUSES or not window.contains(game.start_time):
            continue
        home_key = normalize_team(game.home_abbreviation)
        away_key = normalize_team(game.away_abbreviation)
        if queried and home_key == queried and away_key and away_key != queried:
            labeled.append(LabeledGame(game.start_time, game.away_abbreviation, True))
            continue
        if queried and away_key == queried and home_key and home_key != queried:
            labeled.append(LabeledGame(game.start_time, game.home_abbreviation, False))
            continue
        return None
    return tuple(labeled)


def assemble_home(
    *,
    league_name: str,
    week: int,
    timezone_name: str,
    manager: HomeSide,
    opponent: HomeSide | None,
    roster: Sequence[RosterRow],
    games_status: Literal["ready", "empty", "unavailable"],
    games: Sequence[GameRow],
    recommendation_status: Literal["ready", "empty", "unavailable"],
    recommendation_title: str | None = None,
    recommendation_message: str | None = None,
    recommendation_deadline: datetime | None = None,
) -> dict[str, Any]:
    """Build the public document. Unavailable games contribute no items."""
    if games_status == "unavailable":
        game_items: list[dict[str, Any]] = []
    else:
        game_items = [_game_item(game) for game in games]
    if recommendation_status == "ready":
        recommendation: dict[str, Any] = {
            "status": "ready",
            "title": recommendation_title,
            "message": recommendation_message,
            "deadline": (
                None if recommendation_deadline is None else recommendation_deadline.isoformat()
            ),
        }
    else:
        recommendation = {"status": recommendation_status}
    return {
        "league": {"name": league_name, "week": week, "timezone": timezone_name},
        "matchup": {
            "manager": _side(manager),
            "opponent": None if opponent is None else _side(opponent),
        },
        "roster": [_roster_item(row) for row in roster],
        "games": {"status": games_status, "items": game_items},
        "recommendation": recommendation,
    }


def _side(side: HomeSide) -> dict[str, Any]:
    return {"name": side.name, "score": side.score, "image_url": side.image_url}


def _roster_item(row: RosterRow) -> dict[str, Any]:
    return {
        "slot": row.slot,
        "name": row.name,
        "nba_team": row.nba_team,
        "image_url": player_image_url(row.espn_id),
        "team_image_url": team_image_url(row.nba_team),
    }


def _game_item(game: GameRow) -> dict[str, Any]:
    return {
        "start": game.start.isoformat(),
        "player_name": game.player_name,
        "player_image_url": player_image_url(game.espn_id),
        "opponent": game.opponent,
        "opponent_image_url": team_image_url(game.opponent),
        "home": game.home,
    }
