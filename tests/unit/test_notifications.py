import asyncio

from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.dispatcher import NotificationDispatcher


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.messages.append(notification)
        if self.fail:
            raise RuntimeError("delivery failed")


def test_dispatcher_uses_fallback_after_primary_failure() -> None:
    primary = RecordingSender(fail=True)
    fallback = RecordingSender()
    notification = Notification(title="Lock recommended", message="Lock 50.2 points")

    asyncio.run(NotificationDispatcher(primary, fallback).send(notification))

    assert primary.messages == [notification]
    assert fallback.messages == [notification]
