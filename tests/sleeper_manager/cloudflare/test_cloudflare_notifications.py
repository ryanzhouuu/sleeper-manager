import asyncio
import json
from typing import Any

from sleeper_manager.cloudflare.notifications import (
    CloudflareDiscordSender,
    CloudflareNtfySender,
)
from sleeper_manager.notifications.base import Notification, NotificationAction


class Response:
    ok = True
    status = 200


class RecordingFetch:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Response:
        self.requests.append({"url": url, **kwargs})
        return Response()


def notification() -> Notification:
    return Notification(
        title="Lock player",
        message="Lock the placeholder player",
        actions=(
            NotificationAction(
                label="Locked",
                url="https://example.test/ack?token=token&action=locked",
                method="POST",
            ),
        ),
    )


def test_ntfy_sender_uses_worker_fetch_and_action_payload() -> None:
    fetch = RecordingFetch()
    sender = CloudflareNtfySender(
        "topic",
        base_url="https://ntfy.sh",
        access_token="secret",
        fetcher=fetch,
    )

    asyncio.run(sender.send(notification()))

    request = fetch.requests[0]
    assert request["url"] == "https://ntfy.sh"
    assert request["headers"]["authorization"] == "Bearer secret"
    assert json.loads(request["body"])["actions"][0]["url"].endswith("action=locked")


def test_discord_sender_keeps_action_links() -> None:
    fetch = RecordingFetch()
    sender = CloudflareDiscordSender("https://discord.example/webhook", fetcher=fetch)

    asyncio.run(sender.send(notification()))

    assert (
        "[Locked](https://example.test/ack?token=token&action=locked)"
        in json.loads(fetch.requests[0]["body"])["content"]
    )
