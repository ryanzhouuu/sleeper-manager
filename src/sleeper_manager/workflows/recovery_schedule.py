"""Use durable normal planning work to revisit recoveries before their legal move deadline."""

from datetime import datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.planning import WeeklyPlan
from sleeper_manager.persistence.base import (
    AsyncStateRepository,
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)


async def schedule_recovery_checks(
    repository: AsyncStateRepository, plan: WeeklyPlan, now: datetime
) -> None:
    """Week-end recovery also needs a wake when no later roster game exists."""
    upsert = getattr(repository, "upsert_scheduled_work", None)
    if upsert is None:
        return
    for recovery in plan.recovery_obligations:
        due = recovery.deadline - timedelta(minutes=5)
        if due <= now:
            continue
        identity = f"recovery:{plan.league_id}:{plan.week}:{plan.roster_id}:{recovery.recovery_id}"
        await upsert(
            ScheduledWorkRecord(
                work_id=sha256(identity.encode()).hexdigest()[:32],
                dedupe_key=identity,
                kind=DueWorkKind.DAILY,
                due_at=due,
                status=ScheduledWorkStatus.PENDING,
                deadline=recovery.deadline,
                created_at=now,
                updated_at=now,
            )
        )
