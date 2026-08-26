"""Value types shared by weekly-plan scoring and orchestration.

This private module owns planner configuration and scoring results. Callers
import these types from ``sleeper_manager.decisions.weekly_plan``, which keeps
the stable public surface and class metadata.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite

from sleeper_manager.decisions.lineup import SlotAssignment

WEEKLY_PLANNER_VERSION = "weekly-planner-v1"
DEFAULT_MOVE_LEAD_TIME = timedelta(minutes=10)


class WeeklyPlanError(ValueError):
    """Report that a team-week state cannot produce scoreable options."""


class TerminalValueApproximation(StrEnum):
    """Identify how the planner estimates terminal weekly value."""

    COMMON_BASELINE_MARGINAL = "common_baseline_marginal"
    COMPLETE_ASSIGNMENT_ROLLOUT = "complete_assignment_rollout"


@dataclass(frozen=True, slots=True)
class WeeklyPlanPolicyConfig:
    """Control scenario sampling and deterministic tie handling."""

    scenario_count: int = 2000
    seed: int = 0
    tie_tolerance: float = 0.01

    def __post_init__(self) -> None:
        """Reject invalid sampling counts and comparison tolerances."""
        if self.scenario_count <= 0:
            raise ValueError("Weekly plan scenario count must be positive")
        if not isfinite(self.tie_tolerance) or self.tie_tolerance < 0:
            raise ValueError("Weekly plan tie tolerance must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class PlacementEvaluation:
    """Describe the terminal value of one opportunity-to-slot placement."""

    candidate_id: str
    player_id: str
    game_id: str
    slot_index: int
    slot_position: str
    standalone_expected_value: float
    expected_terminal_value: float
    marginal_terminal_value: float


@dataclass(frozen=True, slots=True)
class WeeklyPlanOption:
    """Summarize one complete assignment for the actionable game batch."""

    assignments: tuple[SlotAssignment, ...]
    expected_terminal_value: float
    marginal_value: float
    move_count: int
    retained_observed_count: int


@dataclass(frozen=True, slots=True)
class WeeklyPlanDecision:
    """Capture ranked options and the evidence used to select between them."""

    decision_time: datetime
    batch_start: datetime
    batch_game_ids: tuple[str, ...]
    baseline_terminal_value: float
    observed_terminal_value: float
    selected: WeeklyPlanOption
    alternative: WeeklyPlanOption | None
    scenario_count: int
    seed: int
    approximation: TerminalValueApproximation
    evaluations: tuple[PlacementEvaluation, ...]
    perfect_information_bound: float
