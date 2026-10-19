"""Carry a bounded wake signal; resolve policy and private evidence inside the consumer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_SCHEMA = "planning-wake-v1"
MAX_WAKE_AGE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class PlanningWake:
    """A duplicate wake can only claim work through the existing durable D1 leases."""

    requested_at: datetime

    def to_body(self) -> str:
        """Use text encoding to keep the Python/JavaScript boundary deterministic."""
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("Planning wake time must be timezone-aware")
        return json.dumps(
            {"schema": _SCHEMA, "requested_at": self.requested_at.astimezone(UTC).isoformat()}
        )

    @classmethod
    def from_body(cls, body: object) -> PlanningWake:
        """Reject unsupported or malformed messages without echoing their contents."""
        if not isinstance(body, str) or len(body) > 256:
            raise ValueError("Invalid planning wake")
        try:
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema", "requested_at"}
                or payload["schema"] != _SCHEMA
                or not isinstance(payload["requested_at"], str)
            ):
                raise ValueError
            requested = datetime.fromisoformat(payload["requested_at"])
            if requested.tzinfo is None or requested.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid planning wake") from error
        return cls(requested.astimezone(UTC))

    def is_current(self, now: datetime) -> bool:
        """Old wakes are redundant because cron keeps publishing current wake signals."""
        return timedelta(0) <= now - self.requested_at <= MAX_WAKE_AGE
