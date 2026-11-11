"""Build local CLI notification senders from `Settings`."""

from sleeper_manager.config import Settings
from sleeper_manager.notifications.discord import DiscordSender
from sleeper_manager.notifications.dispatcher import NotificationDispatcher


def build_notification_dispatcher(settings: Settings) -> NotificationDispatcher:
    """Raises ValueError when no Discord webhook is configured."""
    if settings.discord_webhook_url is None:
        raise ValueError("DISCORD_WEBHOOK_URL is required for notifications")
    return NotificationDispatcher(DiscordSender(settings.discord_webhook_url.get_secret_value()))
