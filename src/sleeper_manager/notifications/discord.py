"""Deliver complete recommendations as ordered Discord message parts when necessary."""

import httpx

from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.parts import split_text


class DiscordSender:
    def __init__(self, webhook_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not webhook_url:
            raise ValueError("Discord webhook URL is required")
        self._webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None

    async def __aenter__(self) -> "DiscordSender":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, notification: Notification) -> None:
        """Stop on a failed part so the dispatcher reports an incomplete delivery."""
        content = f"**{notification.title}**\n{notification.message}"
        if notification.click_url:
            content += f"\n{notification.click_url}"
        if notification.actions:
            content += "\n" + "\n".join(
                f"[{action.label}]({action.url})" for action in notification.actions
            )
        parts = split_text(content, 3800, encoding="utf-16-le")
        for index, part in enumerate(parts):
            prefix = f"Part {index + 1}/{len(parts)}\n" if len(parts) > 1 else ""
            response = await self._client.post(self._webhook_url, json={"content": prefix + part})
            response.raise_for_status()
