"""Deliver Worker notifications through the configured Discord webhook."""

import json
from typing import Any

from sleeper_manager.notifications.base import Notification


class CloudflareDiscordSender:
    def __init__(self, webhook_url: str, *, fetcher: Any) -> None:
        if not webhook_url:
            raise ValueError("Discord webhook URL is required")
        self._webhook_url = webhook_url
        self._fetch = fetcher

    async def send(self, notification: Notification) -> None:
        content = f"**{notification.title}**\n{notification.message}"
        if notification.click_url:
            content += f"\n{notification.click_url}"
        if notification.actions:
            content += "\n" + " | ".join(
                f"[{action.label}]({action.url})" for action in notification.actions
            )
        response = await self._fetch(
            self._webhook_url,
            method="POST",
            body=json.dumps({"content": content}),
            headers={"content-type": "application/json"},
        )
        if not bool(response.ok):
            raise RuntimeError(f"Discord returned HTTP {response.status}")
