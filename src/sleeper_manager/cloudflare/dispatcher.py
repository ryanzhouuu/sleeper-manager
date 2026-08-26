"""Claim due scheduled work and run at most one planning cycle per wake.

Loads runtime policy, ensures today's daily row, then claims. Provider collection
runs only after a successful claim. `scheduled_at` must be timezone-aware.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol

from sleeper_manager.cloudflare.planning import CloudflarePlanningAssembly
from sleeper_manager.cloudflare.scheduler_types import (
    FailureCategory,
    FreshnessDetail,
    ScheduledRunStatus,
    ScheduledRunSummary,
    WorkAttemptSummary,
)
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.runtime_policy import RuntimePolicy, RuntimePolicyError
from sleeper_manager.domain.scheduling import manager_daily_schedule, manager_local_day
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    DueWorkKind,
    RecommendationStatus,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.projections.live_baseline import ProjectionHistoryError
from sleeper_manager.workflows.daily_plan import WeeklyLineupWorkflowResult, run_daily_plan
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs
from sleeper_manager.workflows.pre_tipoff_check import run_pre_tipoff_check

_RETRY_DELAY = timedelta(minutes=5)
_WORKFLOW_STATUS = {
    "no_action": ScheduledRunStatus.NO_ACTION,
    "notified": ScheduledRunStatus.SUCCESS,
    "duplicate": ScheduledRunStatus.DUPLICATE,
    "withdrawn": ScheduledRunStatus.NO_ACTION,
    "blocked": ScheduledRunStatus.BLOCKED,
    "delivery_failed": ScheduledRunStatus.DELIVERY_FAILED,
}


class PlanningCollector(Protocol):
    """Collects live planning evidence for one scheduled wake."""

    async def __call__(
        self,
        *,
        repository: AsyncRuntimeStateRepository,
        policy: RuntimePolicy,
        scheduled_at: datetime,
    ) -> CloudflarePlanningAssembly: ...


async def dispatch_due_work(
    repository: AsyncRuntimeStateRepository,
    *,
    notifications: NotificationLoop,
    collect: PlanningCollector,
    scheduled_at: datetime,
    correlation_id: str,
    open_sleeper_url: str,
    plan_policy: WeeklyPlanPolicyConfig | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ScheduledRunSummary:
    """Dispatch claimed daily, pre-tipoff, and delivery-retry rows for this wake."""
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("Scheduled time must be timezone-aware")
    tick = clock or (lambda: scheduled_at)
    policy = await _load_policy(repository)
    if policy is None:
        return ScheduledRunSummary(
            correlation_id=correlation_id,
            scheduled_at=scheduled_at,
            status=ScheduledRunStatus.BLOCKED,
            claimed_count=0,
            failure_category=FailureCategory.CONFIGURATION,
            detail="runtime_policy_missing",
        )

    await _ensure_daily_work(repository, policy, scheduled_at)
    await repository.cancel_expired_scheduled_work(scheduled_at)
    claimed = await repository.claim_due_work(scheduled_at, correlation_id=correlation_id)
    if not claimed:
        return ScheduledRunSummary(
            correlation_id=correlation_id,
            scheduled_at=scheduled_at,
            status=ScheduledRunStatus.NO_ACTION,
            claimed_count=0,
        )

    planning = tuple(item for item in claimed if item.kind is not DueWorkKind.DELIVERY_RETRY)
    retries = tuple(item for item in claimed if item.kind is DueWorkKind.DELIVERY_RETRY)
    attempts: list[WorkAttemptSummary] = []
    if planning:
        attempts.extend(
            await _run_planning(
                repository,
                planning,
                policy=policy,
                notifications=notifications,
                collect=collect,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                open_sleeper_url=open_sleeper_url,
                plan_policy=plan_policy,
                clock=tick,
            )
        )
    for work in retries:
        attempts.append(
            await _run_delivery_retry(
                repository,
                work,
                notifications=notifications,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                open_sleeper_url=open_sleeper_url,
            )
        )
    return _summary(correlation_id, scheduled_at, len(claimed), tuple(attempts))


async def _load_policy(repository: AsyncRuntimeStateRepository) -> RuntimePolicy | None:
    record = await repository.load_runtime_policy()
    if record is None:
        return None
    try:
        return RuntimePolicy.from_json(record.version, record.payload_json)
    except RuntimePolicyError:
        return None


async def _ensure_daily_work(
    repository: AsyncRuntimeStateRepository,
    policy: RuntimePolicy,
    scheduled_at: datetime,
) -> None:
    local_day = manager_local_day(scheduled_at, timezone_name=policy.manager_timezone)
    due_at = manager_daily_schedule(
        local_day,
        timezone_name=policy.manager_timezone,
        local_time=policy.daily_plan_time,
    )
    key = f"daily:{local_day.isoformat()}"
    await repository.upsert_scheduled_work(
        ScheduledWorkRecord(
            work_id=_work_id(key),
            dedupe_key=key,
            kind=DueWorkKind.DAILY,
            due_at=due_at,
            status=ScheduledWorkStatus.PENDING,
            local_day=local_day.isoformat(),
            created_at=scheduled_at,
            updated_at=scheduled_at,
        )
    )


async def _run_planning(
    repository: AsyncRuntimeStateRepository,
    claimed: tuple[ScheduledWorkRecord, ...],
    *,
    policy: RuntimePolicy,
    notifications: NotificationLoop,
    collect: PlanningCollector,
    scheduled_at: datetime,
    correlation_id: str,
    open_sleeper_url: str,
    plan_policy: WeeklyPlanPolicyConfig | None,
    clock: Callable[[], datetime],
) -> tuple[WorkAttemptSummary, ...]:
    trigger = (
        "pre_tipoff" if any(item.kind is DueWorkKind.PRE_TIPOFF for item in claimed) else "daily"
    )
    try:
        assembly = await collect(
            repository=repository,
            policy=policy,
            scheduled_at=scheduled_at,
        )
    except (ProjectionHistoryError, RuntimePolicyError) as error:
        return await _finish_planning(
            repository,
            claimed,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.BLOCKED,
            failure_category=FailureCategory.CONFIGURATION,
            detail=str(error),
        )
    except Exception as error:
        return await _finish_planning(
            repository,
            claimed,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.FAILED,
            failure_category=FailureCategory.PROVIDER,
            detail=str(error),
            retry=True,
        )

    await _replace_future_pre_tipoff(
        repository,
        policy,
        assembly.evidence.inputs,
        scheduled_at,
    )
    workflow = run_pre_tipoff_check if trigger == "pre_tipoff" else run_daily_plan
    result = await workflow(
        assembly.evidence.inputs,
        decision_time=assembly.evidence.decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url=open_sleeper_url,
        player_names=assembly.player_names,
        local_timezone=assembly.local_timezone,
        policy=plan_policy,
        clock=clock,
    )
    if _delivery_failed(result):
        await _enqueue_delivery_retry(repository, result, scheduled_at)
    outcome = _WORKFLOW_STATUS[result.outcome]
    failure_category = (
        FailureCategory.DELIVERY if outcome is ScheduledRunStatus.DELIVERY_FAILED else None
    )
    return await _finish_planning(
        repository,
        claimed,
        scheduled_at=scheduled_at,
        correlation_id=correlation_id,
        outcome=outcome,
        failure_category=failure_category,
        result=result,
    )


async def _run_delivery_retry(
    repository: AsyncRuntimeStateRepository,
    work: ScheduledWorkRecord,
    *,
    notifications: NotificationLoop,
    scheduled_at: datetime,
    correlation_id: str,
    open_sleeper_url: str,
) -> WorkAttemptSummary:
    recommendation_id = work.recommendation_id
    if not recommendation_id:
        return await _finish_one(
            repository,
            work,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.FAILED,
            failure_category=FailureCategory.INTERNAL_INVARIANT,
            status=ScheduledWorkStatus.CANCELED,
            detail="delivery_retry_missing_recommendation",
        )
    recommendation = await repository.get_recommendation(recommendation_id)
    if recommendation is None or recommendation.status is not RecommendationStatus.PENDING:
        return await _finish_one(
            repository,
            work,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.NO_ACTION,
            status=ScheduledWorkStatus.CANCELED,
            recommendation_id=recommendation_id,
        )
    if await repository.has_successful_delivery(recommendation_id):
        return await _finish_one(
            repository,
            work,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.DUPLICATE,
            status=ScheduledWorkStatus.COMPLETED,
            recommendation_id=recommendation_id,
            recommendation_revision=recommendation.revision,
        )
    result = await notifications.retry_persisted(
        recommendation,
        open_sleeper_url=open_sleeper_url,
    )
    if result.status == "delivery_failed":
        return await _finish_one(
            repository,
            work,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.DELIVERY_FAILED,
            failure_category=FailureCategory.DELIVERY,
            status=ScheduledWorkStatus.RETRY,
            retry_at=scheduled_at + _RETRY_DELAY,
            recommendation_id=recommendation_id,
            recommendation_revision=recommendation.revision,
        )
    outcome = (
        ScheduledRunStatus.DUPLICATE if result.status == "duplicate" else ScheduledRunStatus.SUCCESS
    )
    return await _finish_one(
        repository,
        work,
        scheduled_at=scheduled_at,
        correlation_id=correlation_id,
        outcome=outcome,
        status=ScheduledWorkStatus.COMPLETED,
        recommendation_id=recommendation_id,
        recommendation_revision=recommendation.revision,
    )


async def _replace_future_pre_tipoff(
    repository: AsyncRuntimeStateRepository,
    policy: RuntimePolicy,
    inputs: LivePlanningInputs,
    scheduled_at: datetime,
) -> None:
    wanted = {
        game.provider_id: game
        for game in _roster_games(inputs)
        if game.start_time > scheduled_at and game.status is GameStatus.SCHEDULED
    }
    existing = await repository.list_scheduled_work(
        kind=DueWorkKind.PRE_TIPOFF,
        statuses=(ScheduledWorkStatus.PENDING, ScheduledWorkStatus.RETRY),
    )
    for work in existing:
        if work.game_id in wanted:
            continue
        if work.due_at <= scheduled_at and (work.deadline is None or work.deadline <= scheduled_at):
            continue
        await repository.upsert_scheduled_work(
            replace(work, status=ScheduledWorkStatus.CANCELED, updated_at=scheduled_at)
        )
    for game in wanted.values():
        key = f"pre_tipoff:{game.provider_id}"
        await repository.upsert_scheduled_work(
            ScheduledWorkRecord(
                work_id=_work_id(key),
                dedupe_key=key,
                kind=DueWorkKind.PRE_TIPOFF,
                due_at=game.start_time - policy.move_lead_time,
                status=ScheduledWorkStatus.PENDING,
                game_id=game.provider_id,
                deadline=game.start_time,
                created_at=scheduled_at,
                updated_at=scheduled_at,
            )
        )


def _roster_games(inputs: LivePlanningInputs) -> tuple[ScheduledGame, ...]:
    roster = next(
        item
        for item in inputs.league_profile.rosters
        if item.roster_id == inputs.league_profile.manager_roster_id
    )
    rostered = set(roster.player_ids)
    team_ids = {
        identity.provider_team_id
        for identity in inputs.identities
        if identity.sleeper_player_id in rostered and identity.provider_team_id
    }
    games: dict[str, ScheduledGame] = {}
    for result in inputs.schedule_results:
        for game in result.games:
            if game.home_team_id in team_ids or game.away_team_id in team_ids:
                games[game.provider_id] = game
    return tuple(games.values())


def _delivery_failed(result: WeeklyLineupWorkflowResult) -> bool:
    if result.outcome == "delivery_failed":
        return True
    return result.delivery is not None and not result.delivery.succeeded


async def _enqueue_delivery_retry(
    repository: AsyncRuntimeStateRepository,
    result: WeeklyLineupWorkflowResult,
    scheduled_at: datetime,
) -> None:
    recommendation = result.recommendation
    if recommendation is None:
        return
    key = f"delivery_retry:{recommendation.recommendation_id}"
    await repository.upsert_scheduled_work(
        ScheduledWorkRecord(
            work_id=_work_id(key),
            dedupe_key=key,
            kind=DueWorkKind.DELIVERY_RETRY,
            due_at=scheduled_at,
            status=ScheduledWorkStatus.PENDING,
            recommendation_id=recommendation.recommendation_id,
            deadline=recommendation.deadline,
            created_at=scheduled_at,
            updated_at=scheduled_at,
        )
    )


async def _finish_planning(
    repository: AsyncRuntimeStateRepository,
    claimed: tuple[ScheduledWorkRecord, ...],
    *,
    scheduled_at: datetime,
    correlation_id: str,
    outcome: ScheduledRunStatus,
    failure_category: FailureCategory | None = None,
    detail: str | None = None,
    retry: bool = False,
    result: WeeklyLineupWorkflowResult | None = None,
) -> tuple[WorkAttemptSummary, ...]:
    freshness: tuple[FreshnessDetail, ...] = ()
    if result is not None:
        freshness = tuple(
            FreshnessDetail(
                source.source,
                source.version,
                source.available_as_of,
                source.retrieved_at,
            )
            for source in result.plan.freshness.sources
        )
    attempts = []
    status = ScheduledWorkStatus.RETRY if retry else ScheduledWorkStatus.COMPLETED
    retry_at = scheduled_at + _RETRY_DELAY if retry else None
    for work in claimed:
        attempts.append(
            await _finish_one(
                repository,
                work,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                outcome=outcome,
                failure_category=failure_category,
                status=status,
                retry_at=retry_at,
                detail=detail,
                plan_status=None if result is None else result.plan.status.value,
                recommendation_id=_recommendation_id(result),
                recommendation_revision=_recommendation_revision(result),
                freshness=freshness,
            )
        )
    return tuple(attempts)


async def _finish_one(
    repository: AsyncRuntimeStateRepository,
    work: ScheduledWorkRecord,
    *,
    scheduled_at: datetime,
    correlation_id: str,
    outcome: ScheduledRunStatus,
    status: ScheduledWorkStatus,
    failure_category: FailureCategory | None = None,
    retry_at: datetime | None = None,
    detail: str | None = None,
    plan_status: str | None = None,
    recommendation_id: str | None = None,
    recommendation_revision: int | None = None,
    freshness: tuple[FreshnessDetail, ...] = (),
) -> WorkAttemptSummary:
    attempt = WorkAttemptSummary(
        work_id=work.work_id,
        kind=work.kind,
        outcome=outcome,
        attempt_count=work.attempt_count,
        plan_status=plan_status,
        recommendation_id=recommendation_id,
        recommendation_revision=recommendation_revision,
        failure_category=failure_category,
        freshness=freshness,
    )
    payload = attempt.as_dict()
    if detail:
        payload["detail"] = detail
    await repository.finish_scheduled_work(
        work.work_id,
        status=status,
        finished_at=scheduled_at,
        correlation_id=correlation_id,
        failure_category=None if failure_category is None else failure_category.value,
        terminal_summary_json=json.dumps(payload, sort_keys=True),
        retry_at=retry_at,
    )
    return attempt


def _recommendation_id(result: WeeklyLineupWorkflowResult | None) -> str | None:
    if result is None or result.recommendation is None:
        return None
    return result.recommendation.recommendation_id


def _recommendation_revision(result: WeeklyLineupWorkflowResult | None) -> int | None:
    if result is None or result.recommendation is None:
        return None
    return result.recommendation.revision


def _summary(
    correlation_id: str,
    scheduled_at: datetime,
    claimed_count: int,
    attempts: tuple[WorkAttemptSummary, ...],
) -> ScheduledRunSummary:
    statuses = {attempt.outcome for attempt in attempts}
    if ScheduledRunStatus.FAILED in statuses:
        status = ScheduledRunStatus.FAILED
    elif ScheduledRunStatus.DELIVERY_FAILED in statuses:
        status = ScheduledRunStatus.DELIVERY_FAILED
    elif ScheduledRunStatus.SUCCESS in statuses:
        status = ScheduledRunStatus.SUCCESS
    elif ScheduledRunStatus.DUPLICATE in statuses:
        status = ScheduledRunStatus.DUPLICATE
    elif ScheduledRunStatus.BLOCKED in statuses:
        status = ScheduledRunStatus.BLOCKED
    elif ScheduledRunStatus.NO_ACTION in statuses:
        status = ScheduledRunStatus.NO_ACTION
    else:
        status = ScheduledRunStatus.FAILED
    failure = next(
        (attempt.failure_category for attempt in attempts if attempt.failure_category is not None),
        None,
    )
    return ScheduledRunSummary(
        correlation_id=correlation_id,
        scheduled_at=scheduled_at,
        status=status,
        claimed_count=claimed_count,
        attempts=attempts,
        failure_category=failure,
    )


def _work_id(dedupe_key: str) -> str:
    return sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]


def scheduled_at_from_controller(controller: object | None) -> datetime | None:
    """Read Cron `scheduledTime` as UTC. Values are milliseconds since epoch."""
    if controller is None:
        return None
    value = getattr(controller, "scheduledTime", None)
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000, UTC)


__all__ = (
    "PlanningCollector",
    "dispatch_due_work",
    "scheduled_at_from_controller",
)
