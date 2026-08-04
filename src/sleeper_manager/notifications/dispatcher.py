import logging

from sleeper_manager.notifications.base import Notification, NotificationSender


class NotificationDispatcher:
    def __init__(
        self,
        primary: NotificationSender,
        fallback: NotificationSender | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._logger = logging.getLogger(__name__)

    async def send(self, notification: Notification) -> None:
        try:
            await self._primary.send(notification)
        except Exception:
            self._logger.exception("Primary notification delivery failed")
            if self._fallback is None:
                raise
            await self._fallback.send(notification)
