from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class NotificationAction:
    label: str
    url: str
    method: Literal["GET", "POST", "PUT"] = "POST"
    clear: bool = True
    kind: Literal["http", "view"] = "http"


def ntfy_action(action: NotificationAction) -> dict[str, object]:
    """View actions open the review screen; only background actions carry an HTTP method."""
    payload: dict[str, object] = {
        "action": action.kind,
        "label": action.label,
        "url": action.url,
        "clear": action.clear,
    }
    if action.kind == "http":
        payload["method"] = action.method
    return payload


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


"""Delivery-neutral message actions distinguish browser review from background requests."""
