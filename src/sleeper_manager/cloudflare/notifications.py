import json
from typing import Any

from sleeper_manager.notifications.base import Notification


class CloudflareNtfySender:
    def __init__(self, topic: str, *, base_url: str, access_token: str, fetcher: Any) -> None:
        if not topic:
            raise ValueError("ntfy topic is required")
        self._topic = topic
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._fetch = fetcher

    async def send(self, notification: Notification) -> None:
        payload: dict[str, Any] = {
            "topic": self._topic,
            "title": notification.title,
            "message": notification.message,
            "priority": notification.priority,
            "tags": list(notification.tags),
        }
        if notification.click_url:
            payload["click"] = notification.click_url
        if notification.actions:
            payload["actions"] = [
                {
                    "action": "http",
                    "label": action.label,
                    "url": action.url,
                    "method": action.method,
                    "clear": action.clear,
                }
                for action in notification.actions
            ]
        headers = {"content-type": "application/json"}
        if self._access_token:
            headers["authorization"] = f"Bearer {self._access_token}"
        response = await self._fetch(
            self._base_url,
            method="POST",
            body=json.dumps(payload),
            headers=headers,
        )
        if not bool(response.ok):
            raise RuntimeError(f"ntfy returned HTTP {response.status}")


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
