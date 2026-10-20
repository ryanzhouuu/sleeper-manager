"""Check rendered Discord alerts through the workflow that adds notification actions."""

import asyncio
import json
from dataclasses import replace

import pytest

from sleeper_manager.cloudflare.notifications import CloudflareDiscordSender
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE
from tests.sleeper_manager.cloudflare.test_cloudflare_notifications import RecordingFetch
from tests.sleeper_manager.workflows.test_notification_loop import NOW, request


@pytest.mark.parametrize("decision_type", [WEEKLY_LINEUP_DECISION_TYPE, "placeholder_lock_in"])
def test_discord_advice_omits_sleeper_navigation_and_retains_acknowledgements(
    tmp_path, decision_type: str
) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        fetch = RecordingFetch()
        workflow = NotificationLoop(
            repository,
            NotificationDispatcher(
                CloudflareDiscordSender("https://discord.example/webhook", fetcher=fetch)
            ),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        advice = replace(request(), decision_type=decision_type)

        result = await workflow.run(advice)

        assert result.status == "created"
        assert len(fetch.requests) == 1
        content = json.loads(fetch.requests[0]["body"])["content"]
        assert advice.open_sleeper_url not in content
        assert "Open Sleeper" not in content
        assert content.startswith(f"**{advice.title}**\n{advice.message}")
        if decision_type == WEEKLY_LINEUP_DECISION_TYPE:
            assert content == f"**{advice.title}**\n{advice.message}"
        else:
            assert "[Locked](https://example.test/ack?" in content
            assert "[Passed](https://example.test/ack?" in content

    asyncio.run(exercise())
