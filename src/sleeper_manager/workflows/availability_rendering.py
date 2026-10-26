"""Compact availability for delivery; detailed evidence stays in availability_advice traces."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, tzinfo

from sleeper_manager.domain.planning import PlanningReasonCode, TeamWeekState
from sleeper_manager.workflows.availability_advice import INJURY_DATA_UNAVAILABLE_NOTE

_MISSING_REPORT = "The team has not submitted a game report; participation remains provisional."
_UNCERTAINTY_LABELS = {
    f"{status}; participation is provisional for this game.": status
    for status in ("Questionable", "Doubtful", "Probable")
}
_UNSCOPED_LABELS = {
    f"An unscoped {status} designation is available; its game applicability is unverified "
    "and participation remains provisional.": f"Unverified {status}"
    for status in ("out", "questionable", "doubtful", "probable")
}


def state_availability_summary(
    state: TeamWeekState,
    *,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
) -> tuple[str, ...]:
    """Keep game-scoped warnings distinct and collapse repeated unscoped designations."""
    groups: dict[tuple[datetime, str], set[str]] = defaultdict(set)
    unverified: dict[str, set[str]] = defaultdict(set)
    missing_reports: set[str] = set()
    data_gaps: set[str] = set()
    uncertain = False
    for opportunity in state.opportunities:
        snapshot = opportunity.projection
        if opportunity.scheduled_start <= state.decision_time or snapshot is None:
            continue
        player = opportunity.sleeper_player_id
        for reason in snapshot.reasons:
            if reason.code == "availability_data_gap":
                data_gaps.add(player)
                continue
            if reason.code == "confirmed_game_absence":
                label = "Out"
            elif reason.code == "availability_uncertainty":
                if reason.message == _MISSING_REPORT:
                    missing_reports.add(player)
                    continue
                if reason.message in _UNSCOPED_LABELS:
                    unverified[_UNSCOPED_LABELS[reason.message]].add(player)
                    continue
                label = _UNCERTAINTY_LABELS.get(reason.message, reason.message.rstrip("."))
                uncertain = True
            else:
                continue
            groups[opportunity.scheduled_start, label].add(player)

    names = player_names or {}
    lines: list[str] = []
    if PlanningReasonCode.INJURY_DATA_UNAVAILABLE in state.warnings:
        lines.append(f"• {INJURY_DATA_UNAVAILABLE_NOTE}")
    for (start, label), players in sorted(groups.items()):
        moment = start.astimezone(local_timezone or state.decision_time.tzinfo)
        hour = moment.hour % 12 or 12
        when = f"{moment:%a} {hour}:{moment:%M %p %Z}"
        lines.append(f"• {label}: {_player_names(players, names)} — {when}.")
    for label, players in sorted(unverified.items()):
        lines.append(f"• {label}: {_player_names(players, names)} (game applicability unknown).")
    if missing_reports:
        lines.append(
            f"• Reports missing for {_player_count(missing_reports)}; estimates provisional."
        )
    if data_gaps:
        lines.append(
            f"• Availability unverified for {_player_count(data_gaps)}; estimates provisional."
        )
    if uncertain and not (missing_reports or data_gaps):
        lines.append("• Estimates provisional.")
    return tuple(lines)


def _player_count(players: set[str]) -> str:
    return f"{len(players)} {'player' if len(players) == 1 else 'players'}"


def _player_names(players: set[str], names: Mapping[str, str]) -> str:
    ordered = sorted(players, key=lambda player: (names.get(player, player).casefold(), player))
    return ", ".join(names.get(player, player) for player in ordered)
