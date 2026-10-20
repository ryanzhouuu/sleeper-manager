"""Validation primitives shared by planning evidence and lineup actions."""

from datetime import datetime


class PlanningStateError(ValueError):
    """Reject inconsistent planning evidence or lineup instructions."""


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise PlanningStateError(f"{label} must be timezone-aware")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise PlanningStateError(f"{label} must be non-empty")
