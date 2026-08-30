"""Production-neutral Lock-In decision and trace contracts.

Decision policies, live workflows, and historical replay share these immutable
records. Backtesting may attach chronological ordering evidence, but it does not
own the decision shape consumed by later application layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite


class LockInContractError(ValueError):
    """Report an invalid decision or trace before it enters application state."""


class LockInDecisionKind(StrEnum):
    """Identify the actionable outcomes supported by the current policy boundary."""

    LOCK = "lock"
    PASS = "pass"


@dataclass(frozen=True, slots=True)
class LockInDecision:
    """Capture one policy result with the evidence required to audit it later."""

    decision_time: datetime
    kind: LockInDecisionKind
    player_id: str
    game_id: str
    slot_index: int | None
    expected_terminal_score: float
    counterfactual_value: float
    information_version: str
    reason: str

    def __post_init__(self) -> None:
        """Reject decisions that cannot be applied or reproduced safely."""

        if self.decision_time.tzinfo is None:
            raise LockInContractError("Lock-In decision times must be timezone-aware")
        if not isinstance(self.kind, LockInDecisionKind):
            raise LockInContractError("Lock-In decision kind must be Lock or Pass")
        _require_text(self.player_id, "player ID")
        _require_text(self.game_id, "game ID")
        _require_text(self.information_version, "information version")
        _require_text(self.reason, "decision reason")
        if not isfinite(self.expected_terminal_score) or not isfinite(self.counterfactual_value):
            raise LockInContractError("Lock-In decision values must be finite")
        if self.kind is LockInDecisionKind.LOCK:
            if self.slot_index is None or self.slot_index < 0:
                raise LockInContractError("Lock decisions require a non-negative slot index")
        elif self.slot_index is not None:
            raise LockInContractError("Pass decisions cannot carry a slot index")


@dataclass(frozen=True, slots=True)
class LockInDecisionTrace:
    """Attach stable chronological ordering identity to one exact policy decision."""

    decision: LockInDecision
    event_id: str
    batch_id: str
    candidate_id: str
    evaluation_order: int

    def __post_init__(self) -> None:
        """Reject trace records whose identity cannot be reproduced deterministically."""

        _require_text(self.event_id, "event ID")
        _require_text(self.batch_id, "batch ID")
        _require_text(self.candidate_id, "candidate ID")
        if self.evaluation_order <= 0:
            raise LockInContractError("Lock-In evaluation order must be positive")


def _require_text(value: str, label: str) -> None:
    """Require stable nonblank identifiers and human-readable evidence."""

    if not value.strip():
        raise LockInContractError(f"Lock-In {label} must be non-empty")


__all__ = (
    "LockInContractError",
    "LockInDecision",
    "LockInDecisionKind",
    "LockInDecisionTrace",
)
