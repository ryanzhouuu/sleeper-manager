"""Outbound notification types. Build senders with `factory.build_notification_dispatcher`."""

from sleeper_manager.notifications.base import Notification, NotificationAction

__all__ = ["Notification", "NotificationAction"]
