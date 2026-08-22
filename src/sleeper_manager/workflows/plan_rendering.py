from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import tzinfo

from sleeper_manager.domain.planning import (
    PlanningReasonCode,
    PlanStatus,
    WeeklyPlan,
)

WEEKLY_LINEUP_DECISION_TYPE = "weekly_lineup"

# Deltas smaller than the :.1f display half-step would otherwise render as "0.0 points".
_UNCHANGED_DELTA_EPSILON = 0.05

_TITLES = {
    PlanStatus.ACTION_REQUIRED: "Lineup move required",
    PlanStatus.NO_ACTION: "Lineup is set",
    PlanStatus.DEGRADED: "Lineup check degraded",
    PlanStatus.BLOCKED: "Lineup planning blocked",
}

_REASON_PHRASES = {
    PlanningReasonCode.UNSUPPORTED_SCORING: "the league scoring policy is unsupported",
    PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED: "the league configuration changed",
    PlanningReasonCode.MISSING_ROSTER_SNAPSHOT: "a roster snapshot is missing",
    PlanningReasonCode.ROSTER_STATE_MISMATCH: "roster state could not be reconciled",
    PlanningReasonCode.MISSING_GAME_SCHEDULE: "a game schedule is missing",
    PlanningReasonCode.AMBIGUOUS_GAME_TIME: "a game time is ambiguous",
    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY: "a player identity is unresolved",
    PlanningReasonCode.MISSING_PROJECTION: "a projection is unavailable",
    PlanningReasonCode.PROJECTION_AFTER_DECISION: "a projection arrived after the cutoff",
    PlanningReasonCode.MISSING_MEMBERSHIP: "roster membership is missing",
    PlanningReasonCode.AMBIGUOUS_ELIGIBILITY: "slot eligibility is uncertain",
    PlanningReasonCode.STALE_SLEEPER_STATE: "Sleeper state may be stale",
    PlanningReasonCode.STALE_NBA_STATE: "NBA schedule data may be stale",
    PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT: "acknowledged decisions conflict",
    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER: "event ordering is ambiguous",
    PlanningReasonCode.DEADLINE_ELAPSED: "the lineup deadline has passed",
}


@dataclass(frozen=True, slots=True)
class RenderedPlanNotification:
    title: str
    message: str


def render_weekly_plan(
    plan: WeeklyPlan,
    *,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
) -> RenderedPlanNotification:
    title = _TITLES[plan.status]
    if plan.status is not PlanStatus.ACTION_REQUIRED:
        return RenderedPlanNotification(
            title=title,
            message=_static_message(plan),
        )
    sections = (
        _imperative(plan, player_names),
        _value_clause(plan),
        _risk_clause(plan),
        _deadline_clause(plan, local_timezone),
    )
    return RenderedPlanNotification(
        title=title,
        message=_join_sections("\n\n", *sections),
    )


def _join_sections(separator: str, *sections: str) -> str:
    return separator.join(section for section in sections if section)


def _static_message(plan: WeeklyPlan) -> str:
    if plan.status is PlanStatus.NO_ACTION:
        return "Your lineup already matches the weekly plan."
    if plan.status is PlanStatus.DEGRADED:
        return _join_sections(
            "\n\n",
            "Lineup advice is limited right now.",
            _notes_clause(plan.explanation_reasons + plan.warnings),
            _confidence_clause(plan),
        )
    phrases = _reason_phrases(plan.blocking_reasons)
    blocked_clause = (
        f"Advice is blocked: {'; '.join(phrases)}." if phrases else "Advice is blocked."
    )
    return _join_sections(
        "\n\n",
        blocked_clause,
        "Lineup advice resumes once the issue resolves.",
    )


def _imperative(
    plan: WeeklyPlan,
    player_names: Mapping[str, str] | None,
) -> str:
    labels = _slot_labels(plan)
    phrases = []
    for move in plan.moves:
        name = _player_name(move.player_id, player_names)
        source = labels.get(move.source_slot_index) if move.source_slot_index is not None else None
        target = labels.get(move.target_slot_index) if move.target_slot_index is not None else None
        if source is None and target is not None:
            phrases.append(f"Start {name} in {target}.")
        elif source is not None and target is None:
            phrases.append(f"Move {name} to the bench.")
        else:
            phrases.append(f"Move {name} from {source} to {target}.")
    return " ".join(phrases)


def _value_clause(plan: WeeklyPlan) -> str:
    expected = plan.expected_terminal_score
    observed = plan.observed_terminal_score
    if expected is None or observed is None:
        return ""
    delta = round(expected - observed, 6)
    if abs(delta) < _UNCHANGED_DELTA_EPSILON:
        return "Expected terminal weekly value is unchanged."
    direction = "improves by" if delta > 0 else "drops by"
    return f"Expected terminal weekly value {direction} {abs(delta):.1f} points."


def _risk_clause(plan: WeeklyPlan) -> str:
    return _join_sections(
        "\n",
        _notes_clause(plan.explanation_reasons + plan.warnings),
        _confidence_clause(plan),
        _approximation_clause(plan),
    )


def _confidence_clause(plan: WeeklyPlan) -> str:
    return f"Confidence: {plan.confidence.value}."


def _reason_phrases(reasons: Iterable[PlanningReasonCode]) -> list[str]:
    phrases: list[str] = []
    for reason in reasons:
        phrase = _REASON_PHRASES.get(reason, str(reason.value).replace("_", " "))
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def _notes_clause(reasons: Iterable[PlanningReasonCode]) -> str:
    phrases = _reason_phrases(reasons)
    return f"Notes: {'; '.join(phrases)}." if phrases else ""


def _approximation_clause(plan: WeeklyPlan) -> str:
    summary = plan.distribution_summary
    if summary is None:
        return ""
    return (
        f"Values are modeled estimates over {summary.scenario_count} scenarios "
        f"({summary.approximation}), not guarantees."
    )


def _deadline_clause(plan: WeeklyPlan, local_timezone: tzinfo | None) -> str:
    deadlines = [move.deadline for move in plan.moves]
    if not deadlines:
        return ""
    deadline = min(deadlines).astimezone(local_timezone or plan.decision_time.tzinfo)
    hour12 = deadline.hour % 12 or 12
    zone = deadline.tzname() or str(deadline.tzinfo)
    return (
        f"Complete before {deadline.strftime('%a')} {hour12}:{deadline.strftime('%M %p')} {zone}."
    )


def _slot_labels(plan: WeeklyPlan) -> dict[int, str]:
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


def _player_name(player_id: str, names: Mapping[str, str] | None) -> str:
    if names is None:
        return player_id
    return names.get(player_id, player_id)


__all__ = (
    "WEEKLY_LINEUP_DECISION_TYPE",
    "RenderedPlanNotification",
    "render_weekly_plan",
)
