"""Proposed score actions and recovery obligations stay separate from confirmed decisions."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite

from sleeper_manager.domain._planning_validation import (
    PlanningStateError,
    _require_aware,
    _require_text,
)
from sleeper_manager.domain.lineup_moves import LineupMove
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning_evidence import PlannedAssignment


@dataclass(frozen=True, slots=True)
class PlannedScoreAction:
    """A proposed Lock/Pass never becomes accepted state until manager confirmation."""

    player_id: str
    game_id: str
    kind: LockInDecisionKind
    score: float
    deadline: datetime
    slot_index: int | None = None
    slot_position: str | None = None
    placement_values: tuple[tuple[int, float], ...] = ()
    alternative_expected_score: float | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        _require_text(self.player_id, "Score action player")
        _require_text(self.game_id, "Score action game")
        _require_aware(self.deadline, "Score action deadline")
        if not isfinite(self.score):
            raise PlanningStateError("Score actions require finite scores")
        if (self.kind is LockInDecisionKind.LOCK) != (
            self.slot_index is not None and self.slot_position is not None
        ):
            raise PlanningStateError("Only proposed locks carry an exact position")


@dataclass(frozen=True, slots=True)
class RecoveryObligation:
    """Prove a joint return path that may discard only the named newly added attempts."""

    added_player_games: tuple[tuple[str, str], ...]
    baseline_assignments: tuple[PlannedAssignment, ...]
    attempt_assignments: tuple[PlannedAssignment, ...]
    recovery_at: datetime
    deadline: datetime
    created_at: datetime
    input_version: str
    recovery_moves: tuple[LineupMove, ...] = ()

    def __post_init__(self) -> None:
        for moment in (self.recovery_at, self.deadline, self.created_at):
            _require_aware(moment, "Recovery time")
        if not self.added_player_games or len(set(self.added_player_games)) != len(
            self.added_player_games
        ):
            raise PlanningStateError("Recovery requires distinct added player-games")
        if not self.created_at < self.deadline <= self.recovery_at:
            raise PlanningStateError("Recovery must remain possible before its cutoff")
        _require_text(self.input_version, "Recovery input version")

    @property
    def recovery_id(self) -> str:
        """Keep identity stable across refreshes of the same promised continuation."""
        return sha256(
            repr(
                (
                    self.added_player_games,
                    self.baseline_assignments,
                    self.attempt_assignments,
                    self.recovery_at,
                    self.deadline,
                )
            ).encode()
        ).hexdigest()
