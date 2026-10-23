"""Daily lineup planning workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from typing import Literal

from sleeper_manager.decisions.weekly_plan import (
    WeeklyPlanPolicyConfig,
)
from sleeper_manager.domain.planning import (
    PlanningReasonCode,
    PlanStatus,
    TeamWeekState,
    WeeklyPlan,
)
from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.dispatcher import NotificationDeliveryResult
from sleeper_manager.persistence.base import (
    AsyncStateRepository,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.workflows.availability_advice import (
    append_availability_message,
    append_availability_trace,
    availability_material_hash,
    state_availability_notes,
)
from sleeper_manager.workflows.availability_rendering import state_availability_summary
from sleeper_manager.workflows.current_team_plan import build_current_team_plan
from sleeper_manager.workflows.daily_completion import notify_daily_completion
from sleeper_manager.workflows.notification_loop import (
    Clock,
    NotificationLoop,
    RecommendationRequest,
    recommendation_id_for,
)
from sleeper_manager.workflows.plan_rendering import (
    WEEKLY_LINEUP_DECISION_TYPE,
    RenderedPlanNotification,
    render_weekly_plan,
    serialize_weekly_plan_trace,
)
from sleeper_manager.workflows.planning_inputs import (
    LivePlanningInputs,
)
from sleeper_manager.workflows.recovery_schedule import schedule_recovery_checks
from sleeper_manager.workflows.score_confirmations import prepare_score_confirmations

_WEEKLY_LINEUP_PLACEHOLDER_PLAYER = "weekly-lineup"
_DEFAULT_POLICY = WeeklyPlanPolicyConfig()


@dataclass(frozen=True, slots=True)
class WeeklyLineupWorkflowResult:
    outcome: Literal[
        "no_action",
        "notified",
        "duplicate",
        "withdrawn",
        "blocked",
        "delivery_failed",
    ]
    plan: WeeklyPlan
    recommendation: RecommendationRecord | None
    notification: Notification | None
    delivery: NotificationDeliveryResult | None


async def run_daily_plan(
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
    repository: AsyncStateRepository,
    notifications: NotificationLoop,
    open_sleeper_url: str,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
    policy: WeeklyPlanPolicyConfig | None = None,
    clock: Clock | None = None,
    daily_confirmation: bool = False,
) -> WeeklyLineupWorkflowResult:
    """Optionally confirm scheduled daily completion even when advice requires no action."""
    return await _run_weekly_lineup_workflow(
        inputs,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url=open_sleeper_url,
        player_names=player_names,
        local_timezone=local_timezone,
        policy=policy,
        clock=clock,
        trigger="daily",
        daily_confirmation=daily_confirmation,
    )


async def _run_weekly_lineup_workflow(
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
    repository: AsyncStateRepository,
    notifications: NotificationLoop,
    open_sleeper_url: str,
    player_names: Mapping[str, str] | None,
    local_timezone: tzinfo | None,
    policy: WeeklyPlanPolicyConfig | None,
    clock: Clock | None,
    trigger: Literal["daily", "pre_tipoff", "postgame"],
    prepared_plan: WeeklyPlan | None = None,
    prepared_state: TeamWeekState | None = None,
    daily_confirmation: bool = False,
) -> WeeklyLineupWorkflowResult:
    now = (clock or _utc_now)()
    await repository.expire_recommendations(now)
    plan, state, version = await build_current_team_plan(
        inputs,
        repository,
        decision_time,
        policy or _DEFAULT_POLICY,
        clock or _utc_now,
        prepared_plan=prepared_plan,
        prepared_state=prepared_state,
    )
    await schedule_recovery_checks(repository, plan, now)
    rendered = render_weekly_plan(
        plan,
        player_names=player_names,
        local_timezone=local_timezone,
    )
    notes = state_availability_notes(
        state, player_names=player_names, local_timezone=local_timezone
    )
    summary = state_availability_summary(
        state, player_names=player_names, local_timezone=local_timezone
    )
    rendered = replace(rendered, message=append_availability_message(rendered.message, summary))
    material_hash = availability_material_hash(plan.material_hash, notes)
    trace = append_availability_trace(
        serialize_weekly_plan_trace(plan, trigger=trigger), notes, material_hash
    )
    if version is not None:
        trace = json.dumps(
            {**json.loads(trace), "manager_decision_version": version}, sort_keys=True
        )
    confirmations = await prepare_score_confirmations(
        repository,
        plan,
        message=rendered.message,
        trace_json=trace,
        now=now,
        player_names=dict(player_names) if player_names else None,
    )
    if confirmations:
        payload = json.loads(trace)
        payload["confirmation_targets"] = [row.recommendation_id for row in confirmations]
        trace = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    idempotency_key = await _allocate_idempotency_key(repository, plan, material_hash=material_hash)
    await _supersede_other_pending(
        repository,
        plan.league_id,
        plan.week,
        material_hash=material_hash,
        now=now,
    )

    if _is_quiet_plan(plan):
        recommendation = await _persist_recommendation(
            repository,
            plan=plan,
            rendered=rendered,
            trace=trace,
            idempotency_key=idempotency_key,
            player_id=_WEEKLY_LINEUP_PLACEHOLDER_PLAYER,
            game_id=None,
            deadline=inputs.week_window.ends_at,
            now=now,
        )
        if daily_confirmation:
            completed = await notify_daily_completion(
                recommendation,
                notifications,
                now=(clock or _utc_now)(),
                local_timezone=local_timezone,
                open_sleeper_url=open_sleeper_url,
            )
            if completed is not None:
                return WeeklyLineupWorkflowResult(
                    "notified" if completed.status == "created" else completed.status,
                    plan,
                    completed.recommendation,
                    completed.notification,
                    completed.delivery,
                )
        return WeeklyLineupWorkflowResult("no_action", plan, recommendation, None, None)

    if plan.status is PlanStatus.BLOCKED and _is_deadline_withdrawal(plan):
        recommendation = await _persist_recommendation(
            repository,
            plan=plan,
            rendered=rendered,
            trace=trace,
            idempotency_key=idempotency_key,
            player_id=_primary_player_id(plan),
            game_id=_primary_game_id(plan, state),
            deadline=inputs.week_window.ends_at,
            now=now,
        )
        return WeeklyLineupWorkflowResult("withdrawn", plan, recommendation, None, None)

    player_id = _primary_player_id(plan)
    game_id = _primary_game_id(plan, state)
    deadline = _recommendation_deadline(plan, inputs)
    if not _should_notify(plan, deadline, now):
        recommendation = await _persist_recommendation(
            repository,
            plan=plan,
            rendered=rendered,
            trace=trace,
            idempotency_key=idempotency_key,
            player_id=player_id,
            game_id=game_id,
            deadline=deadline,
            now=now,
        )
        return WeeklyLineupWorkflowResult("withdrawn", plan, recommendation, None, None)

    request = RecommendationRequest(
        league_id=plan.league_id,
        fantasy_week=plan.week,
        player_id=player_id,
        game_id=game_id,
        decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        title=rendered.title,
        message=rendered.message,
        deadline=deadline,
        policy_version=plan.manager_policy_version,
        open_sleeper_url=open_sleeper_url,
        trace_json=trace,
        idempotency_key=idempotency_key,
        acknowledgement_targets=confirmations,
    )
    result = await notifications.run(request)
    outcome: Literal["notified", "duplicate", "blocked", "delivery_failed"]
    if plan.status is PlanStatus.BLOCKED:
        outcome = "duplicate" if result.status == "duplicate" else "blocked"
    elif result.status == "duplicate":
        outcome = "duplicate"
    elif result.status == "delivery_failed":
        outcome = "delivery_failed"
    else:
        outcome = "notified"
    return WeeklyLineupWorkflowResult(
        outcome,
        plan,
        result.recommendation,
        result.notification,
        result.delivery,
    )


def _utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _material_idempotency_base(plan: WeeklyPlan, material_hash: str | None = None) -> str:
    identity = material_hash or plan.material_hash
    return f"{plan.league_id}:{plan.week}:{WEEKLY_LINEUP_DECISION_TYPE}:{identity}"


def _idempotency_key_for_revision(base: str, revision: int) -> str:
    if revision <= 0:
        return base
    return f"{base}:r{revision}"


def _material_hash_from_idempotency_key(key: str) -> str | None:
    marker = f":{WEEKLY_LINEUP_DECISION_TYPE}:"
    if marker not in key:
        return None
    rest = key.split(marker, 1)[1]
    return rest.split(":r", 1)[0]


async def _allocate_idempotency_key(
    repository: AsyncStateRepository,
    plan: WeeklyPlan,
    *,
    material_hash: str | None = None,
) -> str:
    """Mint a fresh key when a prior material plan was superseded or expired."""
    base = _material_idempotency_base(plan, material_hash)
    revision = 0
    while True:
        key = _idempotency_key_for_revision(base, revision)
        existing = await repository.get_recommendation(recommendation_id_for(key))
        if existing is None or existing.status is RecommendationStatus.PENDING:
            return key
        revision += 1


def _is_quiet_plan(plan: WeeklyPlan) -> bool:
    return (plan.status is PlanStatus.NO_ACTION and not plan.score_actions) or (
        plan.status is PlanStatus.DEGRADED and not (plan.moves or plan.score_actions)
    )


def _is_deadline_withdrawal(plan: WeeklyPlan) -> bool:
    return set(plan.blocking_reasons) == {PlanningReasonCode.DEADLINE_ELAPSED}


def _should_notify(plan: WeeklyPlan, deadline: datetime, now: datetime) -> bool:
    if deadline <= now:
        return False
    if plan.status is PlanStatus.BLOCKED:
        return not _is_deadline_withdrawal(plan)
    return plan.status in (PlanStatus.ACTION_REQUIRED, PlanStatus.DEGRADED) and bool(
        plan.moves or plan.score_actions
    )


def _recommendation_deadline(plan: WeeklyPlan, inputs: LivePlanningInputs) -> datetime:
    if plan.moves or plan.score_actions:
        return min(
            [move.deadline for move in plan.moves]
            + [action.deadline for action in plan.score_actions]
        )
    return inputs.week_window.ends_at


def _primary_player_id(plan: WeeklyPlan) -> str:
    if not plan.moves:
        return _WEEKLY_LINEUP_PLACEHOLDER_PLAYER
    move = min(plan.moves, key=lambda item: (item.deadline, item.player_id))
    return move.player_id


def _primary_game_id(plan: WeeklyPlan, state: TeamWeekState) -> str | None:
    player_id = _primary_player_id(plan)
    if player_id == _WEEKLY_LINEUP_PLACEHOLDER_PLAYER:
        return None
    opportunities = [
        opportunity
        for opportunity in state.remaining_opportunities
        if opportunity.sleeper_player_id == player_id
    ]
    if not opportunities:
        return None
    return min(opportunities, key=lambda item: item.scheduled_start).game_id


async def _supersede_other_pending(
    repository: AsyncStateRepository,
    league_id: str,
    fantasy_week: int,
    *,
    material_hash: str,
    now: datetime,
) -> None:
    pending = await repository.list_pending_recommendations(
        league_id,
        fantasy_week,
        decision_type=WEEKLY_LINEUP_DECISION_TYPE,
    )
    for record in pending:
        record_hash = _material_hash_from_idempotency_key(record.idempotency_key)
        if record_hash == material_hash:
            continue
        await repository.supersede_recommendation(record.recommendation_id, now)


async def _persist_recommendation(
    repository: AsyncStateRepository,
    *,
    plan: WeeklyPlan,
    rendered: RenderedPlanNotification,
    trace: str,
    idempotency_key: str,
    player_id: str,
    game_id: str | None,
    deadline: datetime,
    now: datetime,
) -> RecommendationRecord:
    recommendation = RecommendationRecord(
        recommendation_id=recommendation_id_for(idempotency_key),
        idempotency_key=idempotency_key,
        league_id=plan.league_id,
        fantasy_week=plan.week,
        player_id=player_id,
        game_id=game_id,
        decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        title=rendered.title,
        message=rendered.message,
        deadline=deadline,
        policy_version=plan.manager_policy_version,
        created_at=now,
        trace_json=trace,
    )
    await repository.create_recommendation(recommendation)
    existing = await repository.get_recommendation(recommendation.recommendation_id)
    return existing or recommendation


__all__ = ("WeeklyLineupWorkflowResult", "run_daily_plan")
