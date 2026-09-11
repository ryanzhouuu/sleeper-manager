"""Contracts for bounded counterfactual weekly lineup and Lock-In replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from sleeper_manager.backtesting.artifacts import canonical_json, canonicalize, sha256_text
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import TeamWeekComparison, TeamWeekReplayResult
from sleeper_manager.backtesting.replay.projection_surface_models import (
    HistoricalProjectionSurface,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.lock_in import LockInEvaluation
from sleeper_manager.domain.planning import WeeklyPlan

FULL_ADVISOR_EXECUTOR_VERSION = "full-advisor-replay-v3"


class FullAdvisorReplayError(ValueError):
    """Report an input, point-in-time, or legality failure in full replay."""


@dataclass(frozen=True, slots=True)
class FullAdvisorReplayRequest:
    """Configure one deterministic current/current full-advisor replay."""

    team_week: HistoricalTeamWeekInput
    projection_surface: HistoricalProjectionSurface
    weekly_policy_config: WeeklyPlanPolicyConfig = field(default_factory=WeeklyPlanPolicyConfig)
    lock_in_policy_config: LockInPolicyConfig = field(default_factory=LockInPolicyConfig)
    minimum_confidence: float = 0.70
    planning_lead_time: timedelta = timedelta(minutes=10)
    policy_name: str = "current_projection_current_policy"

    def __post_init__(self) -> None:
        """Reject settings that cannot identify a reproducible policy run."""

        if not isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise FullAdvisorReplayError("minimum_confidence must be between zero and one")
        if self.planning_lead_time < timedelta(0):
            raise FullAdvisorReplayError("planning_lead_time cannot be negative")
        if not self.policy_name.strip():
            raise FullAdvisorReplayError("policy_name must be non-empty")


@dataclass(frozen=True, slots=True)
class ReplayLineupTrace:
    """Record the simulated lineup after one chronological transition."""

    event_id: str
    at: datetime
    assignments: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class StartedPlayerGame:
    """Record a player-game that the simulated lineup made Lock-In eligible."""

    player_id: str
    game_id: str
    slot_index: int
    tipoff: datetime


@dataclass(frozen=True, slots=True)
class FullAdvisorReplayExecution:
    """Collect one successful full-advisor replay and its audit traces."""

    status: str
    executor_version: str
    model_result: TeamWeekReplayResult
    oracle_result: TeamWeekReplayResult
    comparison: TeamWeekComparison
    plans: tuple[WeeklyPlan, ...]
    evaluations: tuple[LockInEvaluation, ...]
    lineup_traces: tuple[ReplayLineupTrace, ...]
    started_player_games: tuple[StartedPlayerGame, ...]
    invariant_results: tuple[tuple[str, bool], ...]

    @property
    def fingerprint(self) -> str:
        """Return a stable identity for the complete successful execution."""

        return sha256_text(canonical_json(self))

    def to_dict(self) -> dict[str, Any]:
        """Return canonical JSON-compatible evidence with its content identity."""

        payload = canonicalize(self)
        assert isinstance(payload, dict)
        payload["fingerprint"] = self.fingerprint
        return payload


__all__ = (
    "FULL_ADVISOR_EXECUTOR_VERSION",
    "FullAdvisorReplayError",
    "FullAdvisorReplayExecution",
    "FullAdvisorReplayRequest",
    "ReplayLineupTrace",
    "StartedPlayerGame",
)
