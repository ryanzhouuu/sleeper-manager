"""Roster alignment and matchup scores for the public home document."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from sleeper_manager.cloudflare.home_document import HomeIdentity, RosterRow, TeamRecord

_BENCH_AND_RESERVE = frozenset({"BN", "IR"})


@dataclass(frozen=True, slots=True)
class ParsedSide:
    """One Sleeper matchup side reduced to roster, pairing, and score."""

    roster_id: int
    matchup_id: int | None
    points: float | None


@dataclass(frozen=True, slots=True)
class RosterStanding:
    """Season record and points for one roster. Unparseable settings are None."""

    record: TeamRecord | None
    points_for: float | None


def roster_standings(raw_rosters: Sequence[object]) -> dict[int, RosterStanding]:
    """Read Sleeper roster settings by roster id. Rosters without an integer id are skipped."""
    standings: dict[int, RosterStanding] = {}
    for raw in raw_rosters:
        if not isinstance(raw, Mapping):
            continue
        roster_id = raw.get("roster_id")
        if not _is_integer(roster_id):
            continue
        settings = raw.get("settings")
        if not isinstance(settings, Mapping):
            settings = {}
        wins, losses, ties = (settings.get(key) for key in ("wins", "losses", "ties"))
        record = (
            TeamRecord(wins, losses, ties)
            if _is_integer(wins) and _is_integer(losses) and _is_integer(ties)
            else None
        )
        fpts, decimal = settings.get("fpts"), settings.get("fpts_decimal")
        points_for = (
            round(fpts + decimal / 100, 2) if _is_integer(fpts) and _is_integer(decimal) else None
        )
        standings[roster_id] = RosterStanding(record, points_for)
    return standings


def roster_rows(
    positions: Sequence[str],
    starters: Sequence[str],
    players: Sequence[str],
    reserve: Sequence[str],
    identities: Mapping[str, HomeIdentity],
) -> tuple[RosterRow, ...] | None:
    """Align starters with league slots. None when those sequences differ in length."""
    starting_positions = [
        position.strip().upper()
        for position in positions
        if position.strip().upper() not in _BENCH_AND_RESERVE
    ]
    starter_ids = [_empty_slot(value) for value in starters]
    if len(starting_positions) != len(starter_ids):
        return None
    reserve_ids = [value for value in reserve if _empty_slot(value)]
    occupied = {value for value in starter_ids if value} | set(reserve_ids)
    rows = [
        _roster_row(slot, player_id, identities)
        for slot, player_id in zip(starting_positions, starter_ids, strict=True)
    ]
    rows.extend(
        _roster_row("BN", player_id, identities)
        for player_id in players
        if _empty_slot(player_id) and player_id not in occupied
    )
    rows.extend(_roster_row("IR", player_id, identities) for player_id in reserve_ids)
    return tuple(rows)


def matchup_sides(payload: object) -> tuple[ParsedSide, ...] | None:
    """Parse roster id, matchup id, and numeric points. None when the list is unusable."""
    if not isinstance(payload, list):
        return None
    sides: list[ParsedSide] = []
    for item in payload:
        if not isinstance(item, Mapping):
            return None
        roster_id = item.get("roster_id")
        matchup_id = item.get("matchup_id")
        if isinstance(roster_id, bool) or not isinstance(roster_id, int):
            return None
        if matchup_id is not None and (
            isinstance(matchup_id, bool) or not isinstance(matchup_id, int)
        ):
            return None
        sides.append(ParsedSide(roster_id, matchup_id, _points(item.get("points"))))
    return tuple(sides)


def _empty_slot(value: str) -> str | None:
    normalized = value.strip()
    if not normalized or normalized == "0":
        return None
    return normalized


def _roster_row(
    slot: str,
    player_id: str | None,
    identities: Mapping[str, HomeIdentity],
) -> RosterRow:
    identity = None if player_id is None else identities.get(player_id)
    if identity is None:
        return RosterRow(slot=slot, name=None, nba_team=None, espn_id=None)
    return RosterRow(
        slot=slot,
        name=identity.name,
        nba_team=identity.nba_team,
        espn_id=identity.espn_id,
        positions=identity.positions,
        injury_status=identity.injury_status,
    )


def _is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _points(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
