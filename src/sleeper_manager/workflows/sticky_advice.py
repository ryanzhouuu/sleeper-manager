"""Replace published advice only when a replan clearly disagrees with it."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import PlanStatus, WeeklyPlan
from sleeper_manager.persistence.base import (
    AsyncStateRepository,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE

CONFIDENCE_BAND = 0.05

_Key = tuple[str, str]


async def published_team_advice(
    repository: AsyncStateRepository, league_id: str, week: int, *, now: datetime
) -> tuple[RecommendationRecord, dict[str, Any]] | None:
    """Return live advice the manager has seen; undelivered actions must stay retryable."""
    record = await repository.latest_current_recommendation(
        league_id, week, decision_type=WEEKLY_LINEUP_DECISION_TYPE
    )
    if (
        record is None
        or record.status is not RecommendationStatus.PENDING
        or (record.deadline is not None and record.deadline <= now)
    ):
        return None
    trace = json.loads(record.trace_json)
    if (trace.get("score_actions") or trace.get("moves")) and not (
        await repository.has_successful_delivery(record.recommendation_id)
    ):
        return None
    return record, trace


def retains_published_advice(
    plan: WeeklyPlan,
    *,
    availability_notes: tuple[str, ...],
    published: Mapping[str, Any],
    minimum_confidence: float,
) -> bool:
    """Keep advice when only Lock/Pass stances moved, and none left the confidence band."""
    if not _same_evidence(plan, availability_notes, published):
        return False
    previous = _published_stances(published)
    current = {(row.player_id, row.game_id): row for row in plan.score_actions}
    undecided = {(row.player_id, row.game_id): row for row in plan.undecided_scores}
    if previous is None or previous.keys() != current.keys() | undecided.keys():
        return False
    changed = False
    for key, stance in previous.items():
        action = current.get(key)
        if (action.kind.value if action else None) == stance:
            continue
        changed = True
        if action is not None:
            decisive = (action.confidence or 0) >= minimum_confidence + CONFIDENCE_BAND
        else:
            row = undecided[key]
            held = (
                row.lock_confidence
                if stance == LockInDecisionKind.LOCK.value
                else row.pass_confidence
            )
            decisive = held < minimum_confidence - CONFIDENCE_BAND
        if decisive:
            return False
    return changed


def _published_stances(published: Mapping[str, Any]) -> dict[_Key, str | None] | None:
    """Traces predating undecided evidence cannot prove which finals they considered."""
    if "undecided_scores" not in published:
        return None
    stances: dict[_Key, str | None] = {
        (row["player_id"], row["game_id"]): None for row in published["undecided_scores"]
    }
    stances.update(
        {(row["player_id"], row["game_id"]): row["kind"] for row in published["score_actions"]}
    )
    return stances


def _same_evidence(
    plan: WeeklyPlan, availability_notes: tuple[str, ...], published: Mapping[str, Any]
) -> bool:
    """Require identical manager state and news, so only model noise can explain a difference."""
    if (
        plan.status is PlanStatus.BLOCKED
        or published.get("status") == PlanStatus.BLOCKED.value
        or plan.recovery_obligations
        or plan.forfeited_opportunities
        or plan.recovery_failures
        or published.get("recovery_obligations")
        or published.get("forfeited_opportunities")
        or published.get("recovery_failures")
    ):
        return False
    return (
        tuple(published.get("availability_notes", ())) == availability_notes
        and published.get("planner_version") == plan.planner_version
        and published.get("manager_policy_version") == plan.manager_policy_version
        and tuple(published.get("bench_player_ids", ())) == plan.bench_player_ids
        and _observed(published) == {r.slot_index: r.player_id for r in plan.observed_assignments}
        and _keys(published, "passed_opportunities")
        == {(row.player_id, row.game_id) for row in plan.passed_opportunities}
        and _keys(published, "fixed_slots")
        == {(row.player_id, row.game_id) for row in plan.fixed_slots}
    )


def _observed(published: Mapping[str, Any]) -> dict[int, str | None]:
    return {
        row["slot_index"]: row["player_id"] for row in published.get("observed_assignments", ())
    }


def _keys(published: Mapping[str, Any], field: str) -> set[_Key]:
    return {(row["player_id"], row["game_id"]) for row in published.get(field, ())}
