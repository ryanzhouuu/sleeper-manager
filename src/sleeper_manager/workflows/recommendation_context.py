"""Keep score decisions and required recovery follow-up concise in notifications."""

from collections.abc import Mapping
from datetime import tzinfo

from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import PlanStatus, WeeklyPlan


def score_action_phrases(
    plan: WeeklyPlan, names: Mapping[str, str] | None, kind: LockInDecisionKind | None = None
) -> list[str]:
    """Place locks after starter realignment; a Pass means leaving the game unlocked."""
    labels = slot_labels(plan)
    phrases = []
    for action in plan.score_actions:
        if kind is not None and action.kind is not kind:
            continue
        player = (names or {}).get(action.player_id, action.player_id)
        if action.kind is LockInDecisionKind.LOCK:
            assert action.slot_index is not None
            phrases.append(
                f"Lock {player}'s {action.score:.2f} pts in {labels[action.slot_index]} in Sleeper."
            )
        else:
            phrases.append(f"Pass on {player}'s {action.score:.2f} pts.")
    return phrases


def undecided_phrases(plan: WeeklyPlan, names: Mapping[str, str] | None) -> list[str]:
    """Lay out both sides of each close call so the manager can make it.

    "Better in" is the share of simulations in which that side finishes ahead of the other.
    """
    labels = slot_labels(plan)
    phrases = []
    for row in plan.undecided_scores:
        player = (names or {}).get(row.player_id, row.player_id)
        final = f"{player}'s {row.score:.2f} pts" if row.score is not None else f"{player}'s final"
        if row.lock_expected_score is None or row.pass_expected_score is None:
            if row.score is not None and row.lock_slot_index is None:
                phrases.append(f"{final} stays unlocked; no open slot can take it.")
            else:
                phrases.append(
                    f"Up to you on {final}: lock is better in {row.lock_confidence:.0%} of "
                    f"simulations, pass in {row.pass_confidence:.0%}."
                )
            continue
        assert row.lock_slot_index is not None
        slot = labels.get(row.lock_slot_index, row.lock_slot_position)
        if row.lock_confidence == 0 and row.pass_confidence == 0:
            phrases.append(
                f"Up to you on {final}: locking in {slot} or passing ties in every simulation "
                f"({row.pass_expected_score:.1f} pts projected)."
            )
            continue
        lock = (
            f"Lock in {slot}: {row.lock_expected_score:.1f} pts projected, "
            f"better in {row.lock_confidence:.0%}{_win_suffix(row.lock_win_chance)}"
        )
        passed = (
            f"Pass: {row.pass_expected_score:.1f} pts projected, "
            f"better in {row.pass_confidence:.0%}{_win_suffix(row.pass_win_chance)}"
        )
        phrases.append(f"Up to you on {final}. {lock}. {passed}.")
    return phrases


def _win_suffix(chance: float | None) -> str:
    return "" if chance is None else f", {chance:.0%} to win"


def recommendation_follow_up(
    plan: WeeklyPlan, names: Mapping[str, str] | None, local_timezone: tzinfo | None
) -> str:
    """Retain recovery deadlines and consequences without repeating unchanged slots."""
    labels = names or {}
    positions = slot_labels(plan)
    parts = []
    if plan.status is not PlanStatus.BLOCKED:
        for action in plan.score_actions:
            if action.alternative_expected_score is not None:
                alternative = "pass" if action.kind is LockInDecisionKind.LOCK else "lock"
                parts.append(
                    f"Alternative ({alternative}): {action.alternative_expected_score:.1f} pts."
                )
        for recovery in plan.recovery_obligations:
            players = ", ".join(
                labels.get(player, player) for player, _ in recovery.added_player_games
            )
            moment = recovery.deadline.astimezone(local_timezone or plan.decision_time.tzinfo)
            attempt = {row.slot_index: row.player_id for row in recovery.attempt_assignments}
            restore = "; ".join(
                f"{labels.get(row.player_id, row.player_id)} at {positions[row.slot_index]}"
                if row.player_id
                else f"leave {positions[row.slot_index]} open"
                for row in recovery.baseline_assignments
                if row.player_id != attempt.get(row.slot_index)
            )
            instruction = (
                f"restore {restore} or follow updated advice" if restore else "review advice"
            )
            hour = moment.hour % 12 or 12
            parts.append(
                f"Review before {moment:%a} {hour}:{moment:%M %p %Z}: {instruction}. "
                f"Benching {players} forfeits any active attempt."
            )
        if plan.forfeited_opportunities:
            parts.append(
                "Recovery forfeits: "
                + ", ".join(
                    labels.get(player, player) for player, _ in plan.forfeited_opportunities
                )
                + ". Returning to starters cannot restore these games."
            )
    return "\n\n".join(parts)


def slot_labels(plan: WeeklyPlan) -> dict[int, str]:
    positions = {
        assignment.slot_index: assignment.slot_position for assignment in plan.desired_assignments
    }
    counts: dict[str, int] = {}
    for position in positions.values():
        counts[position] = counts.get(position, 0) + 1
    ordinals: dict[str, int] = {}
    labels: dict[int, str] = {}
    for index in sorted(positions):
        position = positions[index]
        if counts[position] > 1:
            ordinals[position] = ordinals.get(position, 0) + 1
            labels[index] = f"{position} {ordinals[position]}"
        else:
            labels[index] = position
    return labels
