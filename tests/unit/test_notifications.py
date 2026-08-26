import asyncio
import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from sleeper_manager.config import Settings
from sleeper_manager.notifications.base import Notification, NotificationAction
from sleeper_manager.notifications.discord import DiscordSender
from sleeper_manager.notifications.dispatcher import (
    NotificationDeliveryAttempt,
    NotificationDeliveryResult,
    NotificationDispatcher,
)
from sleeper_manager.notifications.factory import build_notification_dispatcher
from sleeper_manager.notifications.ntfy import NtfySender


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.messages.append(notification)
        if self.fail:
            raise RuntimeError("delivery failed")


def _notification() -> Notification:
    return Notification(
        title="Lock recommended",
        message="Lock 50.2 points",
        priority=4,
        tags=("lock",),
        click_url="https://sleeper.example/league",
        actions=(
            NotificationAction(
                label="Locked",
                url="https://ack.example/ack?token=t&action=locked",
            ),
        ),
    )


def test_dispatcher_uses_fallback_after_primary_failure() -> None:
    primary = RecordingSender(fail=True)
    fallback = RecordingSender()
    notification = Notification(title="Lock recommended", message="Lock 50.2 points")

    asyncio.run(NotificationDispatcher(primary, fallback).send(notification))

    assert primary.messages == [notification]
    assert fallback.messages == [notification]


def test_dispatcher_primary_success_skips_fallback() -> None:
    primary = RecordingSender()
    fallback = RecordingSender()
    notification = _notification()

    result = asyncio.run(NotificationDispatcher(primary, fallback).send_with_result(notification))

    assert result.succeeded
    assert fallback.messages == []
    assert result.attempts == (NotificationDeliveryAttempt("RecordingSender", True),)


def test_dispatcher_without_fallback_records_primary_failure() -> None:
    primary = RecordingSender(fail=True)
    notification = _notification()
    dispatcher = NotificationDispatcher(primary)

    result = asyncio.run(dispatcher.send_with_result(notification))

    assert not result.succeeded
    assert result.attempts[0].error == "delivery failed"
    with pytest.raises(RuntimeError, match="delivery failed"):
        asyncio.run(dispatcher.send(notification))


def test_dispatcher_records_when_fallback_also_fails() -> None:
    dispatcher = NotificationDispatcher(RecordingSender(fail=True), RecordingSender(fail=True))
    result = asyncio.run(dispatcher.send_with_result(_notification()))

    assert not result.succeeded
    assert [attempt.succeeded for attempt in result.attempts] == [False, False]


def test_empty_delivery_result_is_unsuccessful() -> None:
    assert not NotificationDeliveryResult(()).succeeded


def test_ntfy_sender_requires_topic() -> None:
    with pytest.raises(ValueError, match="ntfy topic is required"):
        NtfySender("")


def test_discord_sender_requires_webhook() -> None:
    with pytest.raises(ValueError, match="Discord webhook URL is required"):
        DiscordSender("")


@respx.mock
def test_ntfy_sender_posts_actions_click_and_bearer_token() -> None:
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    notification = _notification()

    async def run() -> None:
        async with NtfySender("alerts", access_token="secret") as sender:
            await sender.send(notification)

    asyncio.run(run())

    payload = json.loads(route.calls[0].request.content)
    assert payload["topic"] == "alerts"
    assert payload["title"] == notification.title
    assert payload["click"] == notification.click_url
    assert payload["actions"][0]["label"] == "Locked"
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@respx.mock
def test_ntfy_sender_omits_optional_fields_and_auth() -> None:
    route = respx.post("https://ntfy.example").mock(return_value=httpx.Response(200))

    async def run() -> None:
        async with NtfySender("alerts", base_url="https://ntfy.example/") as sender:
            await sender.send(Notification(title="T", message="M"))

    asyncio.run(run())

    payload = json.loads(route.calls[0].request.content)
    assert "click" not in payload
    assert "actions" not in payload
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
def test_ntfy_sender_raises_for_http_errors() -> None:
    respx.post("https://ntfy.sh").mock(return_value=httpx.Response(500))

    async def run() -> None:
        async with NtfySender("alerts") as sender:
            await sender.send(Notification(title="T", message="M"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


@respx.mock
def test_discord_sender_formats_links_and_actions() -> None:
    route = respx.post("https://discord.example/hook").mock(return_value=httpx.Response(204))

    async def run() -> None:
        async with DiscordSender("https://discord.example/hook") as sender:
            await sender.send(_notification())

    asyncio.run(run())

    content = json.loads(route.calls[0].request.content)["content"]
    assert content.startswith("**Lock recommended**")
    assert "https://sleeper.example/league" in content
    assert "[Locked](https://ack.example/ack?token=t&action=locked)" in content


@respx.mock
def test_discord_sender_raises_for_http_errors() -> None:
    respx.post("https://discord.example/hook").mock(return_value=httpx.Response(400))

    async def run() -> None:
        async with DiscordSender("https://discord.example/hook") as sender:
            await sender.send(Notification(title="T", message="M"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


def test_factory_requires_a_destination() -> None:
    with pytest.raises(ValueError, match="notification destination"):
        build_notification_dispatcher(Settings(_env_file=None))


def test_factory_uses_ntfy_as_primary_when_both_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeNtfy:
        def __init__(self, topic: str, **kwargs: object) -> None:
            captured["ntfy"] = (topic, kwargs)

    class FakeDiscord:
        def __init__(self, webhook_url: str, **kwargs: object) -> None:
            captured["discord"] = webhook_url

    monkeypatch.setattr("sleeper_manager.notifications.factory.NtfySender", FakeNtfy)
    monkeypatch.setattr("sleeper_manager.notifications.factory.DiscordSender", FakeDiscord)
    dispatcher = build_notification_dispatcher(
        Settings(
            _env_file=None,
            ntfy_topic="alerts",
            ntfy_access_token=SecretStr("tok"),
            discord_webhook_url=SecretStr("https://discord.example/hook"),
        )
    )

    assert captured["ntfy"] == (
        "alerts",
        {"base_url": "https://ntfy.sh", "access_token": "tok"},
    )
    assert captured["discord"] == "https://discord.example/hook"
    assert isinstance(dispatcher._primary, FakeNtfy)
    assert isinstance(dispatcher._fallback, FakeDiscord)


def test_factory_uses_discord_when_ntfy_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDiscord:
        def __init__(self, webhook_url: str, **kwargs: object) -> None:
            self.webhook_url = webhook_url

    monkeypatch.setattr("sleeper_manager.notifications.factory.DiscordSender", FakeDiscord)
    dispatcher = build_notification_dispatcher(
        Settings(_env_file=None, discord_webhook_url=SecretStr("https://discord.example/hook"))
    )

    assert isinstance(dispatcher._primary, FakeDiscord)
    assert dispatcher._fallback is None
