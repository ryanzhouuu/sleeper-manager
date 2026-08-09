from datetime import UTC, datetime
from typing import Any

from sleeper_manager.cloudflare.notifications import (
    CloudflareDiscordSender,
    CloudflareNtfySender,
)
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    default_placeholder_request,
)


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


async def run_scheduled(env: Any, fetcher: Any) -> dict[str, Any]:
    acknowledgement_base_url = _value(env, "ACKNOWLEDGEMENT_BASE_URL").rstrip("?")
    if not acknowledgement_base_url:
        return {"status": "skipped", "reason": "acknowledgement_url_missing"}
    try:
        dispatcher = build_dispatcher(env, fetcher)
    except ValueError:
        return {"status": "skipped", "reason": "notifications_not_configured"}

    now = datetime.now(UTC)
    repository = D1StateRepository(env.sleeper_manager_state)
    await repository.initialize()
    result = await NotificationLoop(
        repository,
        dispatcher,
        acknowledgement_base_url=acknowledgement_base_url,
    ).run(
        default_placeholder_request(
            league_id=_value(env, "SLEEPER_LEAGUE_ID", "phase3-cloudflare"),
            now=now,
            open_sleeper_url=_value(env, "OPEN_SLEEPER_URL", "https://sleeper.com"),
        )
    )
    return {
        "status": result.status,
        "recommendation_id": result.recommendation.recommendation_id,
        "delivery_attempts": len(result.delivery.attempts) if result.delivery else 0,
    }
