"""Opaque acknowledgement tokens. Persist only the SHA-256 hash, never the raw token."""

import hashlib
import secrets


def generate_action_token() -> str:
    """Return a URL-safe token suitable for acknowledgement query strings."""
    return secrets.token_urlsafe(32)


def hash_action_token(token: str) -> str:
    """Hash the raw token the same way `/ack` and notification links do."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
