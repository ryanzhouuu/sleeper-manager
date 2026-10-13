"""Explain remaining-game availability and identify material changes to visible advice."""

import json
from collections.abc import Mapping
from datetime import tzinfo
from hashlib import sha256

from sleeper_manager.domain.planning import TeamWeekState


def state_availability_notes(
    state: TeamWeekState,
    *,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
) -> tuple[str, ...]:
    """Present scoped assumptions without exposing diagnostic source identifiers."""
    notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    data_gap = False
    for opportunity in state.opportunities:
        snapshot = opportunity.projection
        if opportunity.scheduled_start <= state.decision_time or snapshot is None:
            continue
        for reason in snapshot.reasons:
            if reason.code == "availability_data_gap":
                data_gap = True
                continue
            if reason.code not in ("confirmed_game_absence", "availability_uncertainty"):
                continue
            key = opportunity.sleeper_player_id, reason.code, reason.message
            if key in seen:
                continue
            seen.add(key)
            player = (player_names or {}).get(
                opportunity.sleeper_player_id, opportunity.sleeper_player_id
            )
            moment = opportunity.scheduled_start
            if local_timezone is not None:
                moment = moment.astimezone(local_timezone)
            notes.append(f"{player} ({moment:%a %H:%M %Z}): {reason.message}")
    if data_gap:
        notes.insert(
            0, "Game availability could not be verified; participation estimates are provisional."
        )
    return tuple(notes)


def availability_material_hash(base: str, notes: tuple[str, ...]) -> str:
    """A changed visible status warning is material; a refreshed identical report is not."""
    return sha256(repr((base, notes)).encode()).hexdigest() if notes else base


def append_availability_message(message: str, notes: tuple[str, ...]) -> str:
    return message + "\n\nAvailability:\n" + "\n".join(notes) if notes else message


def append_availability_trace(trace_json: str, notes: tuple[str, ...], material_hash: str) -> str:
    """Retain the original plan/evaluation trace and separate availability delivery identity."""
    if not notes:
        return trace_json
    payload = json.loads(trace_json)
    if not isinstance(payload, dict):
        raise ValueError("Availability notes require an object trace")
    payload["availability_notes"] = notes
    payload["availability_material_hash"] = material_hash
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
