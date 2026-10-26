"""Render validated weekly actions for notifications and versioned decision traces."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, tzinfo
from typing import Literal

from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import (
    PlanningReasonCode,
    PlanStatus,
    WeeklyPlan,
)
from sleeper_manager.workflows.recommendation_context import (
    recommendation_follow_up,
    score_action_phrases,
)
from sleeper_manager.workflows.recommendation_context import (
    slot_labels as _slot_labels,
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
    PlanningReasonCode.STALE_NBA_STATE: "NBA provider data is unavailable or may be stale",
    PlanningReasonCode.INJURY_DATA_UNAVAILABLE: "injury updates are unavailable",
    PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT: "acknowledged decisions conflict",
    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER: "event ordering is ambiguous",
    PlanningReasonCode.DEADLINE_ELAPSED: "the lineup deadline has passed",
    PlanningReasonCode.RECOVERY_WINDOW_CLOSED: (
        "an earlier recovery window closed; its later-game chance is no longer assumed"
    ),
    PlanningReasonCode.LOW_CONFIDENCE: "the joint decision did not meet the confidence threshold",
    PlanningReasonCode.SEARCH_LIMIT: "the planner reached its bounded search limit",
    PlanningReasonCode.MISSING_TIPOFF_EVIDENCE: "past starter eligibility could not be verified",
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
    """Keep alerts actionable while retaining complete evidence in the separate trace."""
    title = "Lock-In team recommendation" if plan.score_actions else _TITLES[plan.status]
    # Blocked plans suppress instructions even when they retain moves.
    actionable = plan.status in (PlanStatus.ACTION_REQUIRED, PlanStatus.DEGRADED) and bool(
        plan.moves or plan.score_actions
    )
    if not actionable:
        return RenderedPlanNotification(
            title=title,
            message=_join_sections(
                "\n\n",
                _static_message(plan),
                _value_clause(plan),
                _risk_clause(plan),
                recommendation_follow_up(plan, player_names, local_timezone),
            ),
        )
    sections = (
        _deadline_clause(plan, local_timezone),
        _imperative(plan, player_names),
        _value_clause(plan),
        _risk_clause(plan),
        recommendation_follow_up(plan, player_names, local_timezone),
    )
    return RenderedPlanNotification(
        title=title,
        message=_join_sections("\n\n", *sections),
    )


def _join_sections(separator: str, *sections: str) -> str:
    return separator.join(section for section in sections if section)


def _static_message(plan: WeeklyPlan) -> str:
    if plan.status is PlanStatus.NO_ACTION:
        return "Your lineup is set."
    if plan.status is PlanStatus.DEGRADED:
        return "Lineup advice is limited right now."
    phrases = _reason_phrases(plan.blocking_reasons)
    blocked_clause = (
        f"Advice is blocked: {'; '.join(phrases)}." if phrases else "Advice is blocked."
    )
    return blocked_clause


def _imperative(
    plan: WeeklyPlan,
    player_names: Mapping[str, str] | None,
) -> str:
    """Preserve executable move order and place locks after their required moves."""
    labels = _slot_labels(plan)
    phrases = score_action_phrases(plan, player_names, LockInDecisionKind.PASS)
    for index, move in enumerate(plan.moves):
        if index == plan.pre_lock_move_count:
            phrases.extend(score_action_phrases(plan, player_names, LockInDecisionKind.LOCK))
        name = _player_name(move.player_id, player_names)
        source = labels.get(move.source_slot_index) if move.source_slot_index is not None else None
        target = labels.get(move.target_slot_index) if move.target_slot_index is not None else None
        if move.swap_player_id is not None:
            other = _player_name(move.swap_player_id, player_names)
            if source is None:
                phrases.append(f"Start {name} at {target}; bench {other}.")
            elif target is None:
                phrases.append(f"Start {other} at {source}; bench {name}.")
            else:
                phrases.append(f"Swap {name} ({source}) and {other} ({target}).")
        elif source is None and target is not None:
            phrases.append(f"Start {name} at {target}.")
        elif source is not None and target is None:
            phrases.append(f"Move {name} to the bench.")
        else:
            phrases.append(f"Move {name} from {source} to {target}.")
    if plan.pre_lock_move_count == len(plan.moves):
        phrases.extend(score_action_phrases(plan, player_names, LockInDecisionKind.LOCK))
    return "\n".join(f"• {phrase}" for phrase in phrases)


def _value_clause(plan: WeeklyPlan) -> str:
    """Combine the modeled team total, optional lineup delta, and confidence."""
    expected = plan.expected_terminal_score
    observed = plan.observed_terminal_score
    if expected is None or plan.status is PlanStatus.BLOCKED:
        return "Projected week: unavailable."
    total = f"Projected week: {expected:.1f} pts"
    if observed is not None and plan.moves:
        delta = round(expected - observed, 6)
        if abs(delta) < _UNCHANGED_DELTA_EPSILON:
            delta = 0.0
        total += f" ({delta:+.1f})"
    return f"{total} · {plan.confidence.value.title()} confidence."


def _risk_clause(plan: WeeklyPlan) -> str:
    return _notes_clause(plan.explanation_reasons + plan.warnings)


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


def _deadline_clause(plan: WeeklyPlan, local_timezone: tzinfo | None) -> str:
    """Show the earliest action cutoff without implying Pass requires a Sleeper move."""
    deadlines = [move.deadline for move in plan.moves] + [
        action.deadline for action in plan.score_actions
    ]
    if not deadlines:
        return ""
    moment = min(deadlines).astimezone(local_timezone or plan.decision_time.tzinfo)
    action = "Update in Sleeper" if plan.moves else "Decide"
    when = _format_local_time(moment, local_timezone, plan.decision_time.tzinfo)
    return f"{action} before {when}"


def _format_local_time(
    moment: datetime,
    local_timezone: tzinfo | None,
    fallback: tzinfo | None,
) -> str:
    local = moment.astimezone(local_timezone or fallback)
    hour12 = local.hour % 12 or 12
    zone = local.tzname() or str(local.tzinfo)
    return f"{local.strftime('%a')} {hour12}:{local.strftime('%M %p')} {zone}"


def _player_name(player_id: str, names: Mapping[str, str] | None) -> str:
    if names is None:
        return player_id
    return names.get(player_id, player_id)


def serialize_weekly_plan_trace(
    plan: WeeklyPlan,
    *,
    trigger: Literal["daily", "pre_tipoff", "postgame"] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": 3,
        "league_id": plan.league_id,
        "season": plan.season,
        "week": plan.week,
        "roster_id": plan.roster_id,
        "score_actions": [asdict(row) for row in plan.score_actions],
        "pre_lock_move_count": plan.pre_lock_move_count,
        "passes_before_moves": True,
        "recovery_obligations": [
            {
                **asdict(row),
                "recovery_id": row.recovery_id,
                "displaced_players": sorted(
                    {item.player_id for item in row.baseline_assignments if item.player_id}
                    - {item.player_id for item in row.attempt_assignments if item.player_id}
                ),
                "proof": "executable_combined_recovery",
                "opportunity_count": len(row.added_player_games),
            }
            for row in plan.recovery_obligations
        ],
        "forfeited_opportunities": plan.forfeited_opportunities,
        "recovery_failures": plan.recovery_failures,
        "bench_player_ids": plan.bench_player_ids,
        "kind": WEEKLY_LINEUP_DECISION_TYPE,
        "plan_id": plan.plan_id,
        "material_hash": plan.material_hash,
        "status": plan.status.value,
        "planner_version": plan.planner_version,
        "manager_policy_version": plan.manager_policy_version,
        "scoring_policy_version": plan.scoring_policy_version,
        "league_configuration_version": plan.league_configuration_version,
        "projection_model_version": plan.projection_model_version,
        "input_version": plan.input_version,
        "decision_time": plan.decision_time.isoformat(),
        "expected_terminal_score": plan.expected_terminal_score,
        "best_alternative_score": plan.best_alternative_score,
        "observed_terminal_score": plan.observed_terminal_score,
        "decision_margin": plan.decision_margin,
        "confidence": plan.confidence.value,
        "observed_assignments": [
            {
                "slot_index": assignment.slot_index,
                "slot_position": assignment.slot_position,
                "player_id": assignment.player_id,
            }
            for assignment in plan.observed_assignments
        ],
        "desired_assignments": [
            {
                "slot_index": assignment.slot_index,
                "slot_position": assignment.slot_position,
                "player_id": assignment.player_id,
            }
            for assignment in plan.desired_assignments
        ],
        "moves": [
            {
                "kind": "swap" if move.swap_player_id is not None else "move",
                "player_id": move.player_id,
                "swap_player_id": move.swap_player_id,
                "source_slot_index": move.source_slot_index,
                "target_slot_index": move.target_slot_index,
                "deadline": move.deadline.isoformat(),
            }
            for move in plan.moves
        ],
        "fixed_slots": [
            {
                "slot_index": fixed.slot_index,
                "slot_position": fixed.slot_position,
                "player_id": fixed.player_id,
                "game_id": fixed.game_id,
                "accepted_fantasy_score": fixed.accepted_fantasy_score,
                "decision_time": fixed.decision_time.isoformat(),
                "decision_id": fixed.decision_id,
                "provenance": fixed.provenance,
            }
            for fixed in plan.fixed_slots
        ],
        "passed_opportunities": [
            {
                "player_id": passed.player_id,
                "game_id": passed.game_id,
                "decision_time": passed.decision_time.isoformat(),
                "decision_id": passed.decision_id,
                "provenance": passed.provenance,
            }
            for passed in plan.passed_opportunities
        ],
        "freshness": {
            "sources": [
                {
                    "source": source.source,
                    "version": source.version,
                    "available_as_of": source.available_as_of.isoformat(),
                    "retrieved_at": (
                        source.retrieved_at.isoformat() if source.retrieved_at is not None else None
                    ),
                }
                for source in plan.freshness.sources
            ]
        },
        "blocking_reasons": [reason.value for reason in plan.blocking_reasons],
        "warnings": [reason.value for reason in plan.warnings],
        "explanation_reasons": [reason.value for reason in plan.explanation_reasons],
        "schedule_assumptions": list(plan.schedule_assumptions),
    }
    if trigger is not None:
        payload["trigger"] = trigger
    if plan.distribution_summary is not None:
        payload["distribution_summary"] = {
            "scenario_count": plan.distribution_summary.scenario_count,
            "seed": plan.distribution_summary.seed,
            "approximation": plan.distribution_summary.approximation,
            "perfect_information_bound": plan.distribution_summary.perfect_information_bound,
            "postgame_scenario_count": plan.distribution_summary.postgame_scenario_count,
            "minimum_lock_in_confidence": plan.distribution_summary.minimum_lock_in_confidence,
        }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=lambda value: value.isoformat()
    )


__all__ = (
    "WEEKLY_LINEUP_DECISION_TYPE",
    "RenderedPlanNotification",
    "render_weekly_plan",
    "serialize_weekly_plan_trace",
)
