"""Deliver Worker notifications and retain bounded, non-sensitive provider failure details."""

import asyncio
import json
from typing import Any

from sleeper_manager.notifications.base import Notification, ntfy_action


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
            payload["actions"] = [ntfy_action(action) for action in notification.actions]
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
            raise await _ntfy_failure(response)


async def _ntfy_failure(response: Any) -> RuntimeError:
    """Keep numeric error codes and retry delays; provider text may contain private topics."""
    details = [f"ntfy returned HTTP {response.status}"]
    headers = getattr(response, "headers", None)
    if headers is None:
        return RuntimeError(" ".join(details))
    retry = _bounded_integer(headers.get("retry-after"), maximum=604800)
    if retry is not None:
        details.append(f"retry_after_seconds={retry}")
    media_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type == "text/html":
        details.append("response_type=html")
    elif media_type == "application/json":
        details.append("response_type=json")
        length = _bounded_integer(headers.get("content-length"), maximum=8192)
        if length is not None:
            try:
                content = await asyncio.wait_for(response.bytes(), timeout=3)
                if isinstance(content, bytes) and len(content) <= 8192:
                    payload = json.loads(content)
                    code = payload.get("code") if isinstance(payload, dict) else None
                    if type(code) is int and 10000 <= code <= 59999:
                        details.append(f"ntfy_code={code}")
            except Exception:
                pass
    return RuntimeError(" ".join(details))


def _bounded_integer(value: object, *, maximum: int) -> int | None:
    """Accept only short ASCII decimal headers before converting untrusted values."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 9
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    parsed = int(value)
    return parsed if parsed <= maximum else None


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
