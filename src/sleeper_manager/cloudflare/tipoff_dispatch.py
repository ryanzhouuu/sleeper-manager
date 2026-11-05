"""Run claimed lineup reads; these record evidence only and never plan or notify."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sleeper_manager.cloudflare.postgame_dispatch import _finish
from sleeper_manager.cloudflare.scheduler_types import (
    FailureCategory,
    ScheduledRunStatus,
    WorkAttemptSummary,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, ScheduledWorkRecord
from sleeper_manager.workflows.tipoff_evidence import TipoffLineup, record_tipoff_read

TipoffLineupReader = Callable[[], Awaitable[TipoffLineup]]


def tipoff_from_key(dedupe_key: str) -> datetime:
    """Keys are tipoff_evidence:<before|after>:<ISO tipoff>."""
    return datetime.fromisoformat(dedupe_key.split(":", 2)[2])


async def dispatch_tipoff_reads(
    repository: AsyncRuntimeStateRepository,
    work_items: tuple[ScheduledWorkRecord, ...],
    *,
    read_lineup: TipoffLineupReader | None,
    scheduled_at: datetime,
    correlation_id: str,
    clock: Callable[[], datetime],
) -> tuple[WorkAttemptSummary, ...]:
    """A failed read retries until the work's deadline; one read serves every claimed row."""
    lineup: TipoffLineup | None = None
    error: Exception | None = None
    if read_lineup is not None:
        try:
            lineup = await read_lineup()
        except Exception as caught:
            error = caught
    attempts = []
    for work in work_items:
        if lineup is not None:
            await record_tipoff_read(repository, lineup, tipoff=tipoff_from_key(work.dedupe_key))
        attempts.append(
            await _finish(
                repository,
                work,
                scheduled_at=scheduled_at,
                correlation_id=correlation_id,
                outcome=ScheduledRunStatus.NO_ACTION
                if lineup is not None
                else ScheduledRunStatus.FAILED,
                failure=None if lineup is not None else FailureCategory.PROVIDER,
                detail=None if error is None else type(error).__name__,
                retry=lineup is None and read_lineup is not None,
                clock=clock,
            )
        )
    return tuple(attempts)
