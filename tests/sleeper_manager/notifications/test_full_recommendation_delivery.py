"""Long complete advice retains Unicode, every confirmation, and provider failure reporting."""

import asyncio
import json

import httpx
import pytest
import respx

from sleeper_manager.notifications.base import Notification, NotificationAction
from sleeper_manager.notifications.discord import DiscordSender
from sleeper_manager.notifications.ntfy import NtfySender
from sleeper_manager.notifications.parts import split_text


def test_unicode_splitting_preserves_every_character() -> None:
    message = "Lineup 🏀\n" * 1000
    for encoding, budget in (("utf-8", 2000), ("utf-16-le", 3800)):
        pieces = split_text(message, budget, encoding=encoding)
        assert "".join(pieces) == message
        assert all(len(piece.encode(encoding)) <= budget for piece in pieces)


@respx.mock
def test_discord_delivers_all_advice_and_confirmation_links_in_order() -> None:
    route = respx.post("https://discord.example/hook").mock(return_value=httpx.Response(204))
    message = "Complete team lineup 🏀\n" * 200
    actions = tuple(
        NotificationAction(f"Player {i}: Locked", f"https://example.test/ack?token=synthetic-{i}")
        for i in range(8)
    )

    async def run() -> None:
        async with DiscordSender("https://discord.example/hook") as sender:
            await sender.send(Notification("Team recommendation", message, actions=actions))

    asyncio.run(run())
    contents = [json.loads(call.request.content)["content"] for call in route.calls]
    assert len(contents) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 2000 for part in contents)
    combined = "".join(part.split("\n", 1)[1] for part in contents)
    assert message in combined
    assert all(
        any(f"[{action.label}]({action.url})" in part for part in contents) for action in actions
    )


@respx.mock
def test_ntfy_preserves_paired_actions_across_message_parts() -> None:
    route = respx.post("https://ntfy.example").mock(return_value=httpx.Response(200))
    actions = tuple(
        NotificationAction(f"Player {i}: {kind}", f"https://example.test/{i}/{kind}")
        for i in range(3)
        for kind in ("Locked", "Passed")
    )
    message = "Complete lineup 🏀\n" * 300

    async def run() -> None:
        async with NtfySender("synthetic", base_url="https://ntfy.example") as sender:
            await sender.send(Notification("Team recommendation", message, actions=actions))

    asyncio.run(run())
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert all(
        len(part["message"].encode()) <= 2000 and len(part.get("actions", [])) <= 2
        for part in payloads
    )
    assert "".join(part["message"] for part in payloads) == message
    sent = [action["url"] for part in payloads for action in part.get("actions", [])]
    assert sent == [action.url for action in actions]


@respx.mock
def test_a_failed_later_part_is_not_reported_as_success() -> None:
    route = respx.post("https://discord.example/hook").mock(
        side_effect=[httpx.Response(204), httpx.Response(500)]
    )

    async def run() -> None:
        async with DiscordSender("https://discord.example/hook") as sender:
            await sender.send(Notification("Team recommendation", "Lineup\n" * 1000))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert route.call_count == 2
