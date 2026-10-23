"""Render complete targets, confirmed facts, and recovery follow-up alongside actions."""

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
                f"Then lock {player}'s {action.score:.2f} pts in {labels[action.slot_index]}."
            )
        else:
            phrases.append(
                f"Pass on {player}'s {action.score:.2f} pts: choose not to lock this game. "
                "The player may stay starting or be substituted."
            )
    return phrases


def full_recommendation_context(
    plan: WeeklyPlan, names: Mapping[str, str] | None, local_timezone: tzinfo | None
) -> str:
    """Include unchanged slots and locks so a changed alert stands on its own."""
    labels = names or {}
    positions = slot_labels(plan)
    view = (
        plan.observed_assignments if plan.status is PlanStatus.BLOCKED else plan.desired_assignments
    )
    lineup = "\n".join(
        f"{positions[row.slot_index]}: "
        f"{labels.get(row.player_id, row.player_id) if row.player_id else 'Empty'}"
        for row in view
    )
    parts = [
        ("Observed lineup" if plan.status is PlanStatus.BLOCKED else "Complete lineup if followed")
        + ":\n"
        + lineup
    ]
    if plan.bench_player_ids:
        parts.append(
            "Bench: "
            + ", ".join(labels.get(player, player) for player in plan.bench_player_ids)
            + "."
        )
    if plan.fixed_slots:
        parts.append(
            "Already locked (confirmed): "
            + "; ".join(
                f"{labels.get(row.player_id, row.player_id)} — "
                f"{row.accepted_fantasy_score:.2f} pts, {positions[row.slot_index]}, "
                f"game {row.game_id}"
                for row in plan.fixed_slots
            )
            + "."
        )
    if plan.passed_opportunities:
        parts.append(
            "Decided not to lock: "
            + ", ".join(
                f"{labels.get(row.player_id, row.player_id)} (game {row.game_id})"
                for row in plan.passed_opportunities
            )
            + "."
        )
    if plan.status is not PlanStatus.BLOCKED:
        for action in plan.score_actions:
            if action.alternative_expected_score is not None:
                alternative = "pass" if action.kind is LockInDecisionKind.LOCK else "lock"
                parts.append(
                    f"Alternative ({alternative}): {action.alternative_expected_score:.1f} pts."
                )
            if action.placement_values:
                values = "; ".join(
                    f"{positions[index]}: {value:.1f} pts"
                    for index, value in action.placement_values
                )
                parts.append(
                    f"Lock placement totals for {labels.get(action.player_id, action.player_id)}: "
                    f"{values}."
                )
        for recovery in plan.recovery_obligations:
            players = ", ".join(
                labels.get(player, player) for player, _ in recovery.added_player_games
            )
            moment = recovery.deadline.astimezone(local_timezone or plan.decision_time.tzinfo)
            restore = "; ".join(
                f"{labels.get(row.player_id, row.player_id)} in {positions[row.slot_index]}"
                if row.player_id
                else f"leave {positions[row.slot_index]} open"
                for row in recovery.baseline_assignments
            )
            parts.append(
                f"Extra opportunity with later options preserved: {players}. "
                f"Next review: restore {restore}, or accept a freshly evaluated alternative "
                f"before {moment.strftime('%a %b %d, %I:%M %p %Z')}. "
                "If the attempt is still active, recovery explicitly forfeits that game's "
                "eligibility. Perform the moves in Sleeper."
            )
        if plan.forfeited_opportunities:
            parts.append(
                "Recovery forfeits: "
                + ", ".join(
                    f"{labels.get(player, player)} (game {game})"
                    for player, game in plan.forfeited_opportunities
                )
                + ". Returning to the starters cannot revive those performances."
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
