from typing import Any

import httpx

from sleeper_manager.notifications.base import Notification


class NtfySender:
    def __init__(
        self,
        topic: str,
        *,
        base_url: str = "https://ntfy.sh",
        access_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not topic:
            raise ValueError("ntfy topic is required")
        self._topic = topic
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None

    async def __aenter__(self) -> "NtfySender":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

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

        headers = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        response = await self._client.post(self._base_url, json=payload, headers=headers)
        response.raise_for_status()
