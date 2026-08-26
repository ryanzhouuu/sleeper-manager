from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sleeper_manager.cloudflare.dispatcher import dispatch_due_work, scheduled_at_from_controller
from sleeper_manager.cloudflare.notifications import (
    CloudflareDiscordSender,
    CloudflareNtfySender,
)
from sleeper_manager.cloudflare.planning import (
    CloudflarePlanningAssembly,
    collect_cloudflare_planning_inputs,
)
from sleeper_manager.cloudflare.scheduler_types import (
    FailureCategory,
    ScheduledRunStatus,
    ScheduledRunSummary,
)
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.workflows.notification_loop import NotificationLoop


def _value(env: Any, name: str, default: str = "") -> str:
    value = getattr(env, name, default)
    return str(value) if value is not None else default


def build_dispatcher(env: Any, fetcher: Any) -> NotificationDispatcher:
    ntfy_topic = _value(env, "NTFY_TOPIC")
    discord_url = _value(env, "DISCORD_WEBHOOK_URL")
    if not ntfy_topic and not discord_url:
        raise ValueError("At least one notification destination is required")
    ntfy = (
        CloudflareNtfySender(
            ntfy_topic,
            base_url=_value(env, "NTFY_BASE_URL", "https://ntfy.sh"),
            access_token=_value(env, "NTFY_ACCESS_TOKEN"),
            fetcher=fetcher,
        )
        if ntfy_topic
        else None
    )
    discord = CloudflareDiscordSender(discord_url, fetcher=fetcher) if discord_url else None
    if ntfy is not None:
        return NotificationDispatcher(ntfy, discord)
    if discord is not None:
        return NotificationDispatcher(discord)
    raise ValueError("At least one notification destination is required")


async def run_scheduled(
    env: Any,
    fetcher: Any,
    *,
    controller: Any = None,
    scheduled_at: datetime | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    now = scheduled_at or scheduled_at_from_controller(controller) or datetime.now(UTC)
    correlation = correlation_id or uuid4().hex
    acknowledgement_base_url = _value(env, "ACKNOWLEDGEMENT_BASE_URL").rstrip("?")
    if not acknowledgement_base_url:
        return ScheduledRunSummary(
            correlation_id=correlation,
            scheduled_at=now,
            status=ScheduledRunStatus.BLOCKED,
            claimed_count=0,
            failure_category=FailureCategory.CONFIGURATION,
            detail="acknowledgement_url_missing",
        ).as_dict()
    try:
        dispatcher = build_dispatcher(env, fetcher)
    except ValueError:
        return ScheduledRunSummary(
            correlation_id=correlation,
            scheduled_at=now,
            status=ScheduledRunStatus.BLOCKED,
            claimed_count=0,
            failure_category=FailureCategory.CONFIGURATION,
            detail="notifications_not_configured",
        ).as_dict()

    repository = D1StateRepository(env.sleeper_manager_state)
    await repository.initialize()

    async def collect(
        *,
        repository: AsyncRuntimeStateRepository,
        policy: RuntimePolicy,
        scheduled_at: datetime,
    ) -> CloudflarePlanningAssembly:
        return await collect_cloudflare_planning_inputs(
            env,
            fetcher,
            repository=repository,
            policy=policy,
            scheduled_at=scheduled_at,
            clock=lambda: now,
        )

    summary = await dispatch_due_work(
        repository,
        notifications=NotificationLoop(
            repository,
            dispatcher,
            acknowledgement_base_url=acknowledgement_base_url,
            clock=lambda: now,
        ),
        collect=collect,
        scheduled_at=now,
        correlation_id=correlation,
        open_sleeper_url=_value(env, "OPEN_SLEEPER_URL", "https://sleeper.com"),
    )
    return summary.as_dict()
