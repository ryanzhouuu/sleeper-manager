"""Recover still-owned scheduled claims when any later stage fails or is canceled."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta

from sleeper_manager.cloudflare.scheduler_types import (
    FailureCategory,
    ScheduledRunStatus,
    WorkAttemptSummary,
)
from sleeper_manager.cloudflare.stage_logging import timed_stage
from sleeper_manager.persistence.acknowledgements import AcknowledgementQueryError
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)


class ScheduledStageError(RuntimeError):
    """Keep stage and category without retaining provider URLs or private payloads."""

    def __init__(self, stage: str, category: FailureCategory, error: Exception) -> None:
        self.stage = stage
        self.category = _failure_category(error, default=category)
        self.error_type = type(error).__name__
        super().__init__(stage)


def _failure_category(
    error: BaseException, *, default: FailureCategory = FailureCategory.INTERNAL_INVARIANT
) -> FailureCategory:
    if isinstance(error, AcknowledgementQueryError) or "D1_ERROR" in str(error):
        return FailureCategory.PERSISTENCE
    return default


@contextmanager
def scheduled_stage(
    name: str, category: FailureCategory = FailureCategory.INTERNAL_INVARIANT
) -> Iterator[None]:
    """Log a stage boundary and attach sanitized failure context for claim recovery."""
    try:
        with timed_stage(name):
            yield
    except Exception as error:
        raise ScheduledStageError(name, category, error) from error


@asynccontextmanager
async def recover_claimed_work(
    repository: AsyncRuntimeStateRepository,
    claimed: tuple[ScheduledWorkRecord, ...],
    attempts: list[WorkAttemptSummary],
    *,
    correlation_id: str,
    clock: Callable[[], datetime],
) -> AsyncIterator[None]:
    """Retry unfinished owned rows; propagate cancellation and failed recovery writes."""
    try:
        yield
    except (Exception, asyncio.CancelledError) as error:
        category = (
            error.category if isinstance(error, ScheduledStageError) else _failure_category(error)
        )
        context = {
            "stage": error.stage if isinstance(error, ScheduledStageError) else "claimed_work",
            "error_type": (
                error.error_type if isinstance(error, ScheduledStageError) else type(error).__name__
            ),
            "correlation_id": correlation_id,
            "failure_category": category.value,
        }
        print(json.dumps({"event": "scheduled_work_failed", **context}), flush=True)
        finished = {attempt.work_id for attempt in attempts}
        try:
            for work in claimed:
                if work.work_id in finished:
                    continue
                at = clock()
                attempt = WorkAttemptSummary(
                    work_id=work.work_id,
                    kind=work.kind,
                    outcome=ScheduledRunStatus.FAILED,
                    attempt_count=work.attempt_count,
                    failure_category=category,
                )
                saved = await repository.finish_scheduled_work(
                    work.work_id,
                    status=ScheduledWorkStatus.RETRY,
                    finished_at=at,
                    correlation_id=correlation_id,
                    failure_category=category.value,
                    terminal_summary_json=json.dumps(
                        {**attempt.as_dict(), **context}, sort_keys=True
                    ),
                    retry_at=at + timedelta(minutes=5),
                )
                if saved:
                    attempts.append(attempt)
        except Exception as recovery_error:
            print(
                json.dumps(
                    {
                        "event": "scheduled_recovery_failed",
                        "correlation_id": correlation_id,
                        "error_type": type(recovery_error).__name__,
                    }
                ),
                flush=True,
            )
            raise
        if isinstance(error, asyncio.CancelledError):
            raise
