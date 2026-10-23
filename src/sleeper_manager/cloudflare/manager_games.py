"""Derive visible game state without treating advisor output as a manager acknowledgement."""

import json
from datetime import datetime

from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.base import RecommendationRecord, RecommendationStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


def lockable_games(
    records: tuple[LockInOpportunityRecord, ...], context: ManagerContext, now: datetime
) -> tuple[LockInOpportunityRecord, ...]:
    """Retain each player's newest started performance only while its eligibility is verified."""
    latest: dict[str, LockInOpportunityRecord] = {}
    locked = {r.key.player_id for r in records if r.status is Status.ACKNOWLEDGED_LOCKED}
    for row in sorted(records, key=lambda r: (r.scheduled_start, r.key.game_id)):
        if row.scheduled_start < now:
            latest[row.key.player_id] = row
    result = []
    for row in latest.values():
        slot = context.actual_slot(row.key.player_id)
        if (
            row.key.player_id not in locked
            and row.stable_score is not None
            and row.rostered_at_tipoff is True
            and row.status in {Status.ACTIONABLE, Status.FINALIZING, Status.AUTOMATIC_FINAL}
            and row.acknowledged_action is None
            and now < min(row.action_deadline, row.fantasy_week_end)
            and slot is not None
            and eligible_for_slot(row.eligible_positions, slot[1])
        ):
            result.append(row)
    return tuple(sorted(result, key=lambda r: (r.action_deadline, r.key.player_id)))


def recommendation_for(
    row: LockInOpportunityRecord, plan: RecommendationRecord | None, now: datetime
) -> str:
    """Wait means a valid plan proposes no score action; missing evidence stays unavailable."""
    if plan is None or plan.status is RecommendationStatus.SUPERSEDED:
        return "Unavailable"
    if plan.deadline is not None and plan.deadline <= now:
        return "Unavailable"
    try:
        trace = json.loads(plan.trace_json)
        if trace.get("schema_version") != 3 or trace.get("status") not in {
            "action_required",
            "no_action",
            "degraded",
        }:
            return "Unavailable"
        for action in trace["score_actions"]:
            if (action["player_id"], action["game_id"]) == (row.key.player_id, row.key.game_id):
                return {"lock": "Lock", "pass": "Pass", "wait": "Wait"}.get(
                    action["kind"], "Unavailable"
                )
        return "Wait"
    except (ValueError, TypeError, KeyError, AttributeError):
        return "Unavailable"


def recorded_state(
    row: LockInOpportunityRecord,
    now: datetime,
    *,
    context: ManagerContext | None = None,
    records: tuple[LockInOpportunityRecord, ...] = (),
) -> str:
    """Expiry and forfeiture are automatic outcomes, not user-reported Pass actions."""
    if row.status is Status.ACKNOWLEDGED_LOCKED:
        return "Locked in Sleeper"
    if row.status is Status.ACKNOWLEDGED_PASSED:
        return "Passed · recorded by you"
    if row.status is Status.INELIGIBLE:
        return "Passed · eligibility lost"
    if row.status is Status.AUTOMATIC_FINAL and now >= row.fantasy_week_end:
        return "Automatic final score"
    if now >= row.action_deadline or row.status is Status.EXPIRED:
        return "Passed · lock window closed"
    if context is not None and context.actual_slot(row.key.player_id) is None:
        return "Passed · eligibility lost" if row.rostered_at_tipoff is True else "Not lockable"
    if any(
        other.key.player_id == row.key.player_id
        and row.scheduled_start < other.scheduled_start <= now
        for other in records
    ):
        return "Passed · next game started"
    if row.rostered_at_tipoff is not True or row.status is Status.RECONCILIATION_REQUIRED:
        return "Eligibility unverified"
    return "Wait"
