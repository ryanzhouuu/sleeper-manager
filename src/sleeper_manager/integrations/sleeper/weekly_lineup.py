"""Reconcile current ownership with explicitly verified weekly matchup starters.

Departed weekly scorers leave empty owned slots; confirmed scores are reconciled
separately by retained_weekly_locks rather than restoring player ownership here.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sleeper_manager.integrations.sleeper.schemas import SleeperRosterPayload


def with_matchup_starters(
    roster: SleeperRosterPayload,
    matchups: Sequence[Mapping[str, Any]],
    positions: Sequence[str],
) -> SleeperRosterPayload:
    """Require one aligned weekly lineup; never fall back to general-roster starters."""
    matches = [
        row
        for row in matchups
        if type(row.get("roster_id")) is int and row["roster_id"] == roster.roster_id
    ]
    if len(matches) != 1:
        raise ValueError("Sleeper weekly lineup must contain exactly one manager roster")
    raw = matches[0].get("starters")
    slot_count = sum(position.strip().upper() not in {"BN", "IR"} for position in positions)
    if not isinstance(raw, list) or len(raw) != slot_count:
        raise ValueError("Sleeper weekly lineup does not align with starting slots")
    if any(not isinstance(player, str) for player in raw):
        raise ValueError("Sleeper weekly lineup contains an invalid player ID")
    starters = [player.strip() or "0" for player in raw]
    occupied = [player for player in starters if player != "0"]
    if len(set(occupied)) != len(occupied):
        raise ValueError("Sleeper weekly lineup repeats a starting player")
    if set(occupied) & set(roster.reserve or []):
        raise ValueError("Sleeper weekly lineup conflicts with current reserve membership")
    owned = set(roster.players)
    return roster.model_copy(
        update={"starters": [player if player in owned else "0" for player in starters]}
    )
