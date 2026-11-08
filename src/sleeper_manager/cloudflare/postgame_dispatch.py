"""Dispatch coalesced postgame Lock-In work outside the main scheduler module."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sleeper_manager.cloudflare.planning import CloudflarePlanningAssembly
from sleeper_manager.cloudflare.scheduler_types import (
    FailureCategory,
    ScheduledRunStatus,
    WorkAttemptSummary,
)
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.postgame_lock_in import (
    DirectGameSummarySource,
    PostgameLockInResult,
    observe_postgame,
    recommend_postgame_batch,
)
from sleeper_manager.workflows.sleeper_locks import SleeperLockReader, with_sleeper_locks

_RETRY_DELAY = timedelta(minutes=5)


class PostgamePlanningCollector(Protocol):
    """Collect current weekly inputs needed to evaluate a stable final score."""

    async def __call__(
        self,
        *,
        repository: AsyncRuntimeStateRepository,
        policy: RuntimePolicy,
        scheduled_at: datetime,
    ) -> CloudflarePlanningAssembly: ...


async def dispatch_postgame_work(
    repository: AsyncRuntimeStateRepository,
    work_items: tuple[ScheduledWorkRecord, ...],
    *,
    policy: RuntimePolicy,
    notifications: NotificationLoop,
    collect: PostgamePlanningCollector,
    fetch_summary: DirectGameSummarySource | None,
    scheduled_at: datetime,
    correlation_id: str,
    open_sleeper_url: str,
    clock: Callable[[], datetime],
    lock_reader: SleeperLockReader | None = None,
) -> tuple[WorkAttemptSummary, ...]:
    """Persist all due finals before computing a single coordinated plan."""

    if fetch_summary is None:
        return await _finish_all(
            repository,
            work_items,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.BLOCKED,
            failure=FailureCategory.CONFIGURATION,
            detail="direct_game_summary_source_missing",
        )
    try:
        assembly = await collect(
            repository=repository,
            policy=policy,
            scheduled_at=scheduled_at,
        )
        inputs = assembly.evidence.inputs
        await sync_live_lock_in_opportunities(
            inputs,
            repository=repository,
            observed_at=assembly.evidence.decision_time,
        )
        if lock_reader is not None:
            inputs = await with_sleeper_locks(
                inputs,
                reader=lock_reader,
                repository=repository,
                now=assembly.evidence.decision_time,
            )
    except Exception as error:
        return await _finish_all(
            repository,
            work_items,
            scheduled_at=scheduled_at,
            correlation_id=correlation_id,
            outcome=ScheduledRunStatus.FAILED,
            failure=FailureCategory.PROVIDER,
            detail=str(error),
        )
    observed: dict[str, tuple[LockInOpportunityRecord, ...]] = {}
    errors: dict[str, Exception] = {}
    for work in work_items:
        if work.game_id is None:
            continue
        try:
            observed[work.work_id] = await observe_postgame(
                work.game_id,
                inputs,
                decision_time=assembly.evidence.decision_time,
                repository=repository,
                fetch_summary=fetch_summary,
                poll_id=f"{correlation_id}:{work.work_id}",
            )
        except Exception as error:
            errors[work.work_id] = error

    result = PostgameLockInResult("wait")
    stable = tuple(row for rows in observed.values() for row in rows)
    if stable:
        try:
            result = await recommend_postgame_batch(
                inputs,
                stable,
                decision_time=clock(),
                repository=repository,
                notifications=notifications,
                runtime_policy=policy,
                open_sleeper_url=open_sleeper_url,
                player_names=dict(assembly.player_names),
            )
        except Exception as error:
            errors.update({work_id: error for work_id, rows in observed.items() if rows})

    attempts: list[WorkAttemptSummary] = []
    for work in work_items:
        work_error = errors.get(work.work_id)
        failure: FailureCategory | None
        if work.game_id is None:
            outcome = ScheduledRunStatus.FAILED
            failure = FailureCategory.INTERNAL_INVARIANT
            detail = "postgame_work_missing_game"
            retry = False
            recommendation = None
        elif work_error is not None:
            outcome = ScheduledRunStatus.FAILED
            failure = FailureCategory.PROVIDER
            detail = str(work_error)
            retry = True
            recommendation = None
        else:
            game_result = result if observed.get(work.work_id) else PostgameLockInResult("wait")
            outcome = {
                "wait": ScheduledRunStatus.NO_ACTION,
                "notified": ScheduledRunStatus.SUCCESS,
                "duplicate": ScheduledRunStatus.DUPLICATE,
                "unavailable": ScheduledRunStatus.BLOCKED,
                "automatic_final": ScheduledRunStatus.NO_ACTION,
                "delivery_failed": ScheduledRunStatus.DELIVERY_FAILED,
            }[game_result.outcome]
            failure = FailureCategory.DELIVERY if game_result.outcome == "delivery_failed" else None
            detail = None
            retry = await repository.has_open_lock_in_watch(work.game_id, scheduled_at)
            recommendation = game_result.recommendation
            if recommendation is not None and recommendation.game_id != work.game_id:
                recommendation = None
        attempts.append(
            await _finish(
                repository,
                work,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                outcome=outcome,
                failure=failure,
                detail=detail,
                recommendation=recommendation,
                retry=retry,
                clock=clock,
            )
        )
    return tuple(attempts)


async def _finish_all(
    repository: AsyncRuntimeStateRepository,
    work_items: tuple[ScheduledWorkRecord, ...],
    *,
    scheduled_at: datetime,
    correlation_id: str,
    outcome: ScheduledRunStatus,
    failure: FailureCategory | None = None,
    detail: str | None = None,
) -> tuple[WorkAttemptSummary, ...]:
    """Finish every claimed postgame row with the same blocked or failed outcome."""

    attempts: list[WorkAttemptSummary] = []
    for work in work_items:
        attempts.append(
            await _finish(
                repository,
                work,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                outcome=outcome,
                failure=failure,
                detail=detail,
            )
        )
    return tuple(attempts)


async def _finish(
    repository: AsyncRuntimeStateRepository,
    work: ScheduledWorkRecord,
    *,
    scheduled_at: datetime,
    correlation_id: str,
    outcome: ScheduledRunStatus,
    failure: FailureCategory | None = None,
    detail: str | None = None,
    recommendation: object | None = None,
    retry: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> WorkAttemptSummary:
    """Persist one postgame attempt and its next five-minute wake when still open."""

    recommendation_id = getattr(recommendation, "recommendation_id", None)
    revision = getattr(recommendation, "revision", None)
    attempt = WorkAttemptSummary(
        work_id=work.work_id,
        kind=work.kind,
        outcome=outcome,
        attempt_count=work.attempt_count,
        recommendation_id=recommendation_id,
        recommendation_revision=revision,
        failure_category=failure,
    )
    payload = attempt.as_dict()
    if detail:
        payload["detail"] = detail
    finished_at = (clock or (lambda: scheduled_at))()
    await repository.finish_scheduled_work(
        work.work_id,
        status=ScheduledWorkStatus.RETRY if retry else ScheduledWorkStatus.COMPLETED,
        finished_at=finished_at,
        correlation_id=correlation_id,
        failure_category=failure.value if failure else None,
        terminal_summary_json=json.dumps(payload, sort_keys=True),
        retry_at=scheduled_at + _RETRY_DELAY if retry else None,
    )
    return attempt


__all__ = ["PostgamePlanningCollector", "dispatch_postgame_work"]
