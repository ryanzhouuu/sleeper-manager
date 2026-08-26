from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkRecord

type DueWork = ScheduledWorkRecord


class ScheduledRunStatus(StrEnum):
    SUCCESS = "success"
    NO_ACTION = "no_action"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    DELIVERY_FAILED = "delivery_failed"
    FAILED = "failed"


class FailureCategory(StrEnum):
    CONFIGURATION = "configuration"
    PROVIDER = "provider"
    PERSISTENCE = "persistence"
    DELIVERY = "delivery"
    INTERNAL_INVARIANT = "internal_invariant"


@dataclass(frozen=True, slots=True)
class FreshnessDetail:
    source: str
    version: str
    available_as_of: datetime
    retrieved_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "version": self.version,
            "available_as_of": self.available_as_of.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
        }


@dataclass(frozen=True, slots=True)
class WorkAttemptSummary:
    work_id: str
    kind: DueWorkKind
    outcome: ScheduledRunStatus
    attempt_count: int
    plan_status: str | None = None
    recommendation_id: str | None = None
    recommendation_revision: int | None = None
    failure_category: FailureCategory | None = None
    freshness: tuple[FreshnessDetail, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "work_id": self.work_id,
            "kind": self.kind.value,
            "outcome": self.outcome.value,
            "attempt_count": self.attempt_count,
            "plan_status": self.plan_status,
            "recommendation_id": self.recommendation_id,
            "recommendation_revision": self.recommendation_revision,
            "failure_category": (
                self.failure_category.value if self.failure_category is not None else None
            ),
            "freshness": [item.as_dict() for item in self.freshness],
        }


@dataclass(frozen=True, slots=True)
class ScheduledRunSummary:
    correlation_id: str
    scheduled_at: datetime
    status: ScheduledRunStatus
    claimed_count: int
    attempts: tuple[WorkAttemptSummary, ...] = ()
    failure_category: FailureCategory | None = None
    detail: str | None = None

    @property
    def requires_failed_event(self) -> bool:
        return self.failure_category in {
            FailureCategory.CONFIGURATION,
            FailureCategory.PERSISTENCE,
            FailureCategory.INTERNAL_INVARIANT,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "correlation_id": self.correlation_id,
            "scheduled_at": self.scheduled_at.isoformat(),
            "status": self.status.value,
            "claimed_count": self.claimed_count,
            "failure_category": (
                self.failure_category.value if self.failure_category is not None else None
            ),
            "detail": self.detail,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


__all__ = (
    "DueWork",
    "DueWorkKind",
    "FailureCategory",
    "FreshnessDetail",
    "ScheduledRunStatus",
    "ScheduledRunSummary",
    "WorkAttemptSummary",
)
