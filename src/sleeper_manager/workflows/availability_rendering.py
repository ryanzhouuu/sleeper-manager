"""Compact availability for delivery; detailed evidence stays in availability_advice traces."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, tzinfo

from sleeper_manager.domain.planning import TeamWeekState

_MISSING_REPORT = "The team has not submitted a game report; participation remains provisional."
_UNCERTAINTY_LABELS = {
    f"{status}; participation is provisional for this game.": status
    for status in ("Questionable", "Doubtful", "Probable")
} | {
    f"An unscoped {status} designation is available; its game applicability is unverified "
    "and participation remains provisional.": f"Unverified {status} designation"
    for status in ("out", "questionable", "doubtful", "probable")
}


def state_availability_summary(
    state: TeamWeekState,
    *,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
) -> tuple[str, ...]:
    """Group identical game warnings and count unique players with missing evidence."""
    groups: dict[tuple[datetime, str], set[str]] = defaultdict(set)
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
                uncertain = True
                label = _UNCERTAINTY_LABELS.get(reason.message, reason.message.rstrip("."))
            else:
                continue
            groups[opportunity.scheduled_start, label].add(player)

    names = player_names or {}
    lines: list[str] = []
    for (start, label), players in sorted(groups.items()):
        moment = start.astimezone(local_timezone or state.decision_time.tzinfo)
        hour = moment.hour % 12 or 12
        when = f"{moment:%a %b %d}, {hour}:{moment:%M %p %Z}"
        ordered = sorted(players, key=lambda player: (names.get(player, player).casefold(), player))
        lines.append(
            f"• {label}: {', '.join(names.get(player, player) for player in ordered)} — {when}."
        )
    if missing_reports:
        lines.append(
            f"• Game reports missing for {_player_count(missing_reports)}; "
            "participation estimates are provisional."
        )
    if data_gaps:
        lines.append(
            f"• Game availability could not be verified for {_player_count(data_gaps)}; "
            "participation estimates are provisional."
        )
    if uncertain and not (missing_reports or data_gaps):
        lines.append("• Participation estimates are provisional.")
    return tuple(lines)


def _player_count(players: set[str]) -> str:
    return f"{len(players)} {'player' if len(players) == 1 else 'players'}"
