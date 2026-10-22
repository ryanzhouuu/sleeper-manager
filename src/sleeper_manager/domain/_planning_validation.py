"""Validation primitives shared by planning evidence and lineup actions."""

from collections.abc import Iterable
from datetime import datetime


class PlanningStateError(ValueError):
    """Reject inconsistent planning evidence or lineup instructions."""


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise PlanningStateError(f"{label} must be timezone-aware")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise PlanningStateError(f"{label} must be non-empty")


def _require_unique(values: Iterable[str] | Iterable[int], label: str) -> None:
    materialized: tuple[str | int, ...] = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise PlanningStateError(f"{label} must be unique")
    for value in materialized:
        if isinstance(value, str):
            _require_text(value, label)
