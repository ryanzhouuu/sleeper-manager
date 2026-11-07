"""Parse Sleeper's private ``matchup_legs_raw`` rows, the only source of submitted locks.

The GraphQL endpoint is undocumented, so every field the app relies on is validated here and
a shape change fails loudly instead of reading as "no locks".
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class MatchupLegsError(ValueError):
    """Raised when a matchup-leg payload cannot be trusted as lock state."""


@dataclass(frozen=True, slots=True)
class MatchupLeg:
    """One roster's week: slot-ordered starters and its locked ``player_id -> game_id`` pairs.

    ``points`` is null until Sleeper scores the finished week.
    """

    roster_id: int
    matchup_id: int | None
    points: float | None
    starters: tuple[str, ...]
    starters_games: Mapping[str, str]


def parse_matchup_legs(payload: object) -> tuple[MatchupLeg, ...]:
    if not isinstance(payload, list):
        raise MatchupLegsError("Sleeper matchup legs must be a list")
    legs = tuple(_leg(row) for row in payload)
    roster_ids = [leg.roster_id for leg in legs]
    if len(set(roster_ids)) != len(roster_ids):
        raise MatchupLegsError("Sleeper matchup legs repeat a roster")
    return legs


def _leg(row: object) -> MatchupLeg:
    if not isinstance(row, Mapping):
        raise MatchupLegsError("Sleeper matchup leg must be an object")
    roster_id = _integer(row.get("roster_id"), "roster_id")
    if roster_id is None:
        raise MatchupLegsError("Sleeper matchup leg has no roster_id")
    starters = row.get("starters")
    if not isinstance(starters, list) or not all(isinstance(pid, str) for pid in starters):
        raise MatchupLegsError("Sleeper matchup leg starters must be a list of player ids")
    return MatchupLeg(
        roster_id=roster_id,
        matchup_id=_integer(row.get("matchup_id"), "matchup_id"),
        points=_points(row.get("points")),
        starters=tuple(starters),
        starters_games=_starters_games(row.get("starters_games")),
    )


def _integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MatchupLegsError(f"Sleeper matchup leg {field} must be an integer")
    return value


def _points(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MatchupLegsError("Sleeper matchup leg points must be a number")
    return float(value)


def _starters_games(value: object) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or not all(
        isinstance(pid, str) and isinstance(gid, str) for pid, gid in value.items()
    ):
        raise MatchupLegsError("Sleeper starters_games must map player ids to game ids")
    return MappingProxyType(dict(value))
