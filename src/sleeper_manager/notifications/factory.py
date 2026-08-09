from sleeper_manager.config import Settings
from sleeper_manager.notifications.discord import DiscordSender
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.notifications.ntfy import NtfySender


def build_notification_dispatcher(settings: Settings) -> NotificationDispatcher:
    if not settings.notifications_configured:
        raise ValueError("At least one notification destination is required")
    ntfy = (
        NtfySender(
            settings.ntfy_topic,
            base_url=settings.ntfy_base_url,
            access_token=(
                settings.ntfy_access_token.get_secret_value()
                if settings.ntfy_access_token is not None
                else None
            ),
        )
        if settings.ntfy_topic
        else None
    )
    discord = (
        DiscordSender(settings.discord_webhook_url.get_secret_value())
        if settings.discord_webhook_url is not None
        else None
    )
    if ntfy is not None:
        return NotificationDispatcher(ntfy, discord)
    if discord is not None:
        return NotificationDispatcher(discord)
    raise ValueError("At least one notification destination is required")
