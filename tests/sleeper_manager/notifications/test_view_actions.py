"""View actions open a page instead of submitting a background request."""

import asyncio
import json

import httpx
import respx

from sleeper_manager.cloudflare.notifications import CloudflareNtfySender
from sleeper_manager.notifications.base import Notification, NotificationAction
from sleeper_manager.notifications.ntfy import NtfySender
from tests.sleeper_manager.cloudflare.test_cloudflare_notifications import RecordingFetch


def review_message() -> Notification:
    return Notification(
        title="Review",
        message="Lock advice",
        actions=(
            NotificationAction(
                "Open dashboard",
                "https://dashboard.test/",
                method="GET",
                kind="view",
                clear=False,
            ),
        ),
    )


def test_worker_ntfy_opens_review_instead_of_posting_token():
    fetch = RecordingFetch()
    sender = CloudflareNtfySender(
        "topic", base_url="https://ntfy.example", access_token="", fetcher=fetch
    )
    asyncio.run(sender.send(review_message()))
    action = json.loads(fetch.requests[0]["body"])["actions"][0]
    assert action["action"] == "view" and action["clear"] is False
    assert "method" not in action


@respx.mock
def test_local_ntfy_opens_review_instead_of_posting_token():
    route = respx.post("https://ntfy.example").mock(return_value=httpx.Response(200))

    async def send():  # type: ignore[no-untyped-def]
        async with NtfySender("topic", base_url="https://ntfy.example") as sender:
            await sender.send(review_message())

    asyncio.run(send())
    action = json.loads(route.calls[0].request.content)["actions"][0]
    assert action["action"] == "view" and "method" not in action
