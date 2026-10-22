"""Detect overdue daily planning independently of attempt count and lease transitions."""

import json
from datetime import datetime, timedelta

from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scheduling import (
    manager_daily_schedule,
    manager_local_day,
    manager_local_day_window,
)
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

_DAILY_DELAY_THRESHOLD = timedelta(minutes=15)


async def notify_delayed_daily_planning(
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    *,
    league_id: str,
    scheduled_at: datetime,
    open_sleeper_url: str,
) -> None:
    """Alert once after fifteen minutes without completion, including missing or active work."""
    if not league_id:
        return
    record = await repository.load_runtime_policy()
    if record is None:
        return
    policy = RuntimePolicy.from_json(record.version, record.payload_json)
    day = manager_local_day_window(scheduled_at, timezone_name=policy.manager_timezone)
    local_date = manager_local_day(scheduled_at, timezone_name=policy.manager_timezone)
    original_due_at = manager_daily_schedule(
        local_date, timezone_name=policy.manager_timezone, local_time=policy.daily_plan_time
    )
    if scheduled_at < original_due_at + _DAILY_DELAY_THRESHOLD:
        return
    local_day = local_date.isoformat()
    work = await repository.list_scheduled_work(
        kind=DueWorkKind.DAILY,
    )
    item = next((row for row in work if row.dedupe_key == f"daily:{local_day}"), None)
    if item is not None and (
        item.status in (ScheduledWorkStatus.COMPLETED, ScheduledWorkStatus.CANCELED)
        or (item.deadline is not None and item.deadline <= scheduled_at)
    ):
        return
    await notifications.run(
        RecommendationRequest(
            league_id=league_id,
            fantasy_week=0,
            player_id="system",
            game_id=None,
            decision_type=DAILY_PLAN_DELAYED_KIND,
            title="Daily plan delayed",
            message=(
                "Daily planning has not completed within 15 minutes of its scheduled time. "
                "Check Sleeper directly while the assistant retries."
            ),
            deadline=day.ends_at,
            policy_version=policy.version,
            open_sleeper_url=open_sleeper_url,
            idempotency_key=f"daily-plan-delayed:{league_id}:{day.starts_at.isoformat()}",
            trace_json=json.dumps(
                {
                    "work_id": item.work_id if item is not None else None,
                    "attempt_count": item.attempt_count if item is not None else 0,
                    "original_due_at": original_due_at.isoformat(),
                },
                sort_keys=True,
            ),
        )
    )
