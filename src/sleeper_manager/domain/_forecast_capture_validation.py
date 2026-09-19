"""Shared validation primitives for immutable forecast evidence contracts.

The public contracts live in `forecast_capture`; this module keeps their repeated
identity and timestamp checks consistent without exposing persistence behavior.
"""

from datetime import datetime

_SHA256_LENGTH = 64


class ForecastCaptureError(ValueError):
    """Raised when forecast evidence violates the capture contract."""


def require_text(value: str, label: str) -> None:
    """Reject blank identity and diagnostic strings."""

    if not value.strip():
        raise ForecastCaptureError(f"{label} must be non-empty")


def require_aware(value: datetime, label: str) -> None:
    """Reject timestamps that cannot participate in cutoff comparisons."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ForecastCaptureError(f"{label} must be timezone-aware")


def require_sha256(value: str, label: str) -> None:
    """Validate lowercase SHA-256 identities used for immutable deduplication."""

    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ForecastCaptureError(f"{label} must be a lowercase SHA-256 digest")


__all__ = ("ForecastCaptureError", "require_aware", "require_sha256", "require_text")
