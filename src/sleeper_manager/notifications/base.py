"""Shared message records and action serialization for notification adapters."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationAction:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    click_url: str | None = None
    actions: tuple[NotificationAction, ...] = ()


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...
