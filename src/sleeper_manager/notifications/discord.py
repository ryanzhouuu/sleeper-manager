import httpx

from sleeper_manager.notifications.base import Notification


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
        content = f"**{notification.title}**\n{notification.message}"
        if notification.click_url:
            content += f"\n{notification.click_url}"
        if notification.actions:
            content += "\n" + " | ".join(
                f"[{action.label}]({action.url})" for action in notification.actions
            )
        response = await self._client.post(self._webhook_url, json={"content": content})
        response.raise_for_status()
