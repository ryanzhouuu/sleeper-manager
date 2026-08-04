from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class NotificationAction:
    label: str
    url: str
    method: Literal["GET", "POST", "PUT"] = "POST"
    clear: bool = True


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    priority: int = 3
    tags: tuple[str, ...] = ()
    click_url: str | None = None
    actions: tuple[NotificationAction, ...] = ()


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...
