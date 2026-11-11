"""Split complete advice for provider limits without dropping text."""

from typing import Literal


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
