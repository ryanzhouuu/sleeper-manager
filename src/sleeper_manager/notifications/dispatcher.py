import logging
from dataclasses import dataclass

from sleeper_manager.notifications.base import Notification, NotificationSender


@dataclass(frozen=True, slots=True)
class NotificationDeliveryAttempt:
    provider: str
    succeeded: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationDeliveryResult:
    attempts: tuple[NotificationDeliveryAttempt, ...]

    @property
    def succeeded(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].succeeded


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
        result = await self.send_with_result(notification)
        if not result.succeeded:
            error = result.attempts[-1].error if result.attempts else "delivery failed"
            raise RuntimeError(error or "notification delivery failed")

    async def send_with_result(
        self,
        notification: Notification,
    ) -> NotificationDeliveryResult:
        attempts: list[NotificationDeliveryAttempt] = []
        try:
            await self._primary.send(notification)
        except Exception as error:
            self._logger.exception("Primary notification delivery failed")
            attempts.append(
                NotificationDeliveryAttempt(
                    provider=type(self._primary).__name__,
                    succeeded=False,
                    error=str(error),
                )
            )
            if self._fallback is None:
                return NotificationDeliveryResult(tuple(attempts))
            try:
                await self._fallback.send(notification)
            except Exception as fallback_error:
                self._logger.exception("Fallback notification delivery failed")
                attempts.append(
                    NotificationDeliveryAttempt(
                        provider=type(self._fallback).__name__,
                        succeeded=False,
                        error=str(fallback_error),
                    )
                )
                return NotificationDeliveryResult(tuple(attempts))
            attempts.append(
                NotificationDeliveryAttempt(
                    provider=type(self._fallback).__name__,
                    succeeded=True,
                )
            )
            return NotificationDeliveryResult(tuple(attempts))
        attempts.append(
            NotificationDeliveryAttempt(
                provider=type(self._primary).__name__,
                succeeded=True,
            )
        )
        return NotificationDeliveryResult(tuple(attempts))
