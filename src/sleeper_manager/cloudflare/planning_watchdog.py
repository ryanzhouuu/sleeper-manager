"""Detect repeated unfinished daily work before this wake spends its budget on collection."""

import json
from datetime import datetime

from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scheduling import manager_local_day, manager_local_day_window
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    DueWorkKind,
    ScheduledWorkStatus,
)
from sleeper_manager.workflows.notification_loop import (
    DAILY_PLAN_DELAYED_KIND,
    NotificationLoop,
    RecommendationRequest,
)


async def notify_delayed_daily_planning(
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    *,
    league_id: str,
    scheduled_at: datetime,
    open_sleeper_url: str,
) -> None:
    """Alert once per local day after two attempts; active leases and completed work stay quiet."""
    if not league_id:
        return
    record = await repository.load_runtime_policy()
    if record is None:
        return
    policy = RuntimePolicy.from_json(record.version, record.payload_json)
    day = manager_local_day_window(scheduled_at, timezone_name=policy.manager_timezone)
    local_day = manager_local_day(scheduled_at, timezone_name=policy.manager_timezone).isoformat()
    work = await repository.list_scheduled_work(
        kind=DueWorkKind.DAILY,
        statuses=(ScheduledWorkStatus.RUNNING, ScheduledWorkStatus.RETRY),
    )
    for item in work:
        if item.attempt_count < 2 or item.due_at > scheduled_at:
            continue
        if item.local_day != local_day or (
            item.deadline is not None and item.deadline <= scheduled_at
        ):
            continue
        if item.status is ScheduledWorkStatus.RUNNING and (
            item.lease_expires_at is None or item.lease_expires_at > scheduled_at
        ):
            continue
        await notifications.run(
            RecommendationRequest(
                league_id=league_id,
                fantasy_week=0,
                player_id="system",
                game_id=None,
                decision_type=DAILY_PLAN_DELAYED_KIND,
                title="Daily plan delayed",
                message=(
                    "Daily planning is delayed after repeated attempts. "
                    "Check Sleeper directly while the assistant retries."
                ),
                deadline=day.ends_at,
                policy_version=policy.version,
                open_sleeper_url=open_sleeper_url,
                idempotency_key=f"daily-plan-delayed:{league_id}:{day.starts_at.isoformat()}",
                trace_json=json.dumps(
                    {"work_id": item.work_id, "attempt_count": item.attempt_count}, sort_keys=True
                ),
            )
        )
