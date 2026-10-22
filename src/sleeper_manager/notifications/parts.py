"""Split complete advice for provider limits without dropping text or confirmation actions."""

from dataclasses import replace
from typing import Literal

from sleeper_manager.notifications.base import Notification


def split_text(
    text: str, limit: int, *, encoding: Literal["utf-8", "utf-16-le"]
) -> tuple[str, ...]:
    """Preserve Unicode and whitespace; prefer complete lines within the byte budget."""
    parts = []
    offset = 0
    while offset < len(text):
        end = offset
        size = 0
        while end < len(text) and size + len(text[end].encode(encoding)) <= limit:
            size += len(text[end].encode(encoding))
            end += 1
        if end == offset:
            raise ValueError("Message budget cannot hold a single character")
        if end < len(text):
            boundary = text.rfind("\n", offset, end)
            if boundary < offset:
                boundary = text.rfind(" ", offset, end)
            if boundary > offset:
                end = boundary + 1
        parts.append(text[offset:end])
        offset = end
    return tuple(parts) or ("",)


def ntfy_parts(notification: Notification) -> tuple[Notification, ...]:
    """Keep each Locked/Passed pair together below ntfy's three-action limit."""
    text = split_text(notification.message, 2000, encoding="utf-8")
    groups = tuple(
        notification.actions[i : i + 2] for i in range(0, len(notification.actions), 2)
    ) or ((),)
    count = max(len(text), len(groups))
    return tuple(
        replace(
            notification,
            title=f"{notification.title} ({i + 1}/{count})" if count > 1 else notification.title,
            message=text[i]
            if i < len(text)
            else "Additional confirmation actions for this recommendation.",
            actions=groups[i] if i < len(groups) else (),
        )
        for i in range(count)
    )
