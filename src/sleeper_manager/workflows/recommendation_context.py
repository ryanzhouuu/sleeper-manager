"""Keep score decisions and required recovery follow-up concise in notifications."""

from collections.abc import Mapping
from datetime import tzinfo

from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import PlanStatus, WeeklyPlan


def score_action_phrases(
    plan: WeeklyPlan, names: Mapping[str, str] | None, kind: LockInDecisionKind | None = None
) -> list[str]:
    """Place locks after starter realignment and distinguish Pass from a Sleeper action."""
    labels = slot_labels(plan)
    phrases = []
    for action in plan.score_actions:
        if kind is not None and action.kind is not kind:
            continue
        player = (names or {}).get(action.player_id, action.player_id)
        if action.kind is LockInDecisionKind.LOCK:
            assert action.slot_index is not None
            phrases.append(
                f"Lock {player}'s {action.score:.2f} pts in {labels[action.slot_index]} "
                "in Sleeper, then confirm."
            )
        else:
            phrases.append(
                f"Record Pass on {player}'s {action.score:.2f} pts; substitution is optional."
            )
    return phrases


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
