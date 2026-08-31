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
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.postgame_lock_in import (
    DirectGameSummarySource,
    run_postgame_lock_in,
)

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
) -> tuple[WorkAttemptSummary, ...]:
    """Collect once, fetch each game summary once, and retain five-minute watches."""

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
    attempts: list[WorkAttemptSummary] = []
    for work in work_items:
        if work.game_id is None:
            attempts.append(
                await _finish(
                    repository,
                    work,
                    scheduled_at=scheduled_at,
                    correlation_id=correlation_id,
                    outcome=ScheduledRunStatus.FAILED,
                    failure=FailureCategory.INTERNAL_INVARIANT,
                    detail="postgame_work_missing_game",
                    retry=False,
                )
            )
            continue
        try:
            result = await run_postgame_lock_in(
                work.game_id,
                assembly.evidence.inputs,
                decision_time=assembly.evidence.decision_time,
                repository=repository,
                notifications=notifications,
                fetch_summary=fetch_summary,
                runtime_policy=policy,
                open_sleeper_url=open_sleeper_url,
                poll_id=f"{correlation_id}:{work.work_id}",
                player_names=dict(assembly.player_names),
            )
            outcome = {
                "wait": ScheduledRunStatus.NO_ACTION,
                "notified": ScheduledRunStatus.SUCCESS,
                "duplicate": ScheduledRunStatus.DUPLICATE,
                "unavailable": ScheduledRunStatus.BLOCKED,
                "automatic_final": ScheduledRunStatus.NO_ACTION,
                "delivery_failed": ScheduledRunStatus.DELIVERY_FAILED,
            }[result.outcome]
            retry = await repository.has_open_lock_in_watch(work.game_id, scheduled_at)
            failure = FailureCategory.DELIVERY if result.outcome == "delivery_failed" else None
            attempts.append(
                await _finish(
                    repository,
                    work,
                    scheduled_at=scheduled_at,
                    correlation_id=correlation_id,
                    outcome=outcome,
                    failure=failure,
                    recommendation=result.recommendation,
                    retry=retry,
                    clock=clock,
                )
            )
        except Exception as error:
            attempts.append(
                await _finish(
                    repository,
                    work,
                    scheduled_at=scheduled_at,
                    correlation_id=correlation_id,
                    outcome=ScheduledRunStatus.FAILED,
                    failure=FailureCategory.PROVIDER,
                    detail=str(error),
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
