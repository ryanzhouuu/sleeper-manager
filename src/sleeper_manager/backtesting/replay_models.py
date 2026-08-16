from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from sleeper_manager.domain.projection import ProjectionSnapshot


class ReplayGameStatus(StrEnum):
    SCHEDULED = "scheduled"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ReplayGame:
    game_id: str
    start_time: datetime
    final_time: datetime | None
    week: int
    team_ids: tuple[str, str]
    status: ReplayGameStatus


@dataclass(frozen=True, slots=True)
class ReplayPlayerGame:
    sleeper_id: str
    provider_player_id: str
    game_id: str
    fantasy_team_id: int
    rostered_at_tipoff: bool
    eligible_positions: tuple[str, ...]
    actual_score: float
    projection: ProjectionSnapshot | None = None
    membership_segment: str | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.actual_score):
            raise ValueError("Replay scores must be finite")


@dataclass(frozen=True, slots=True)
class LockCandidate:
    sleeper_id: str
    fantasy_team_id: int
    game_id: str
    completed_score: float
    eligible_positions_at_tipoff: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LockedSlot:
    slot_index: int
    slot_position: str
    sleeper_id: str
    game_id: str
    score: float
    locked_at: datetime


@dataclass(frozen=True, slots=True)
class ReplayDecision:
    decision_time: datetime
    kind: str
    player_id: str
    game_id: str | None
    slot_index: int | None
    information_version: str
    expected_terminal_score: float
    counterfactual_value: float
    reason: str


@dataclass(frozen=True, slots=True)
class TeamWeekReplayResult:
    league_id: str
    week: int
    roster_id: int
    policy_name: str
    realized_score: float
    decisions: tuple[ReplayDecision, ...]
    locked_slots: tuple[LockedSlot, ...]
    automatic_final_scores: tuple[tuple[str, float], ...]
    eligibility_quality: str
    data_quality: str
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamWeekComparison:
    oracle_team_score: float
    model_policy_team_score: float
    lock_in_regret: float
    score_capture: float | None
    invariant_results: tuple[tuple[str, bool], ...]

    def __post_init__(self) -> None:
        if self.lock_in_regret < -1e-6:
            raise ValueError("Lock-In regret cannot be negative")
        if self.score_capture is not None and not 0 <= self.score_capture <= 1 + 1e-6:
            raise ValueError("Score capture must be between zero and one")


__all__ = (
    "LockCandidate",
    "LockedSlot",
    "ReplayDecision",
    "ReplayGame",
    "ReplayGameStatus",
    "ReplayPlayerGame",
    "TeamWeekComparison",
    "TeamWeekReplayResult",
)
