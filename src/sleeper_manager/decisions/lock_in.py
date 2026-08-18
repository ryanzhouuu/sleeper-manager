from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sleeper_manager.backtesting.replay.models import LockedSlot, ReplayPlayerGame
from sleeper_manager.decisions.lineup import AssignmentCandidate, AssignmentResult
from sleeper_manager.decisions.simulation import (
    generate_scenarios,
    pregame_assignment,
    rollout_terminal_score,
    stable_scenario_seed,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.models import Recommendation, RecommendationKind


@dataclass(frozen=True, slots=True)
class LockInContext:
    player_id: str
    completed_score: float
    remaining_game_projections: tuple[float, ...]
    matchup_margin: float | None = None


class LockInPolicy(Protocol):
    def evaluate(self, context: LockInContext) -> Recommendation: ...


@dataclass(frozen=True, slots=True)
class LockInPolicyConfig:
    scenario_count: int = 2000
    fixture_scenario_count: int = 200
    seed: int = 0
    tie_tolerance: float = 0.01

    def __post_init__(self) -> None:
        if self.scenario_count <= 0 or self.fixture_scenario_count <= 0:
            raise ValueError("Scenario counts must be positive")
        if self.tie_tolerance < 0:
            raise ValueError("Tie tolerance must be non-negative")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    kind: str
    player_id: str
    game_id: str
    slot_index: int | None
    expected_terminal_score: float
    counterfactual_value: float
    information_version: str
    reason: str


class ScoreMaximizingLockInPolicy:
    """Scenario-rollout policy that optimizes own-team terminal fantasy score."""

    def __init__(self, config: LockInPolicyConfig | None = None) -> None:
        self.config = config or LockInPolicyConfig()

    def evaluate(self, context: LockInContext) -> Recommendation:
        future_best = max(context.remaining_game_projections, default=float("-inf"))
        should_lock = context.completed_score + self.config.tie_tolerance >= future_best
        kind = RecommendationKind.LOCK if should_lock else RecommendationKind.PASS
        confidence = (
            1.0
            if future_best == float("-inf")
            else min(max(abs(context.completed_score - future_best) / 10, 0.0), 1.0)
        )
        return Recommendation(
            recommendation_id=f"lock-in:{context.player_id}:{kind.value}",
            kind=kind,
            player_id=context.player_id,
            message=(
                "Lock the completed score because it is not below the projected future upside."
                if should_lock
                else "Pass because a projected future game has meaningful own-score upside."
            ),
            confidence=confidence,
        )

    def choose_pregame_assignment(
        self,
        player_games: tuple[ReplayPlayerGame, ...],
        *,
        decision_time: datetime,
        starter_slots: tuple[str, ...],
    ) -> AssignmentResult:
        return pregame_assignment(
            player_games,
            decision_time=decision_time,
            starter_slots=starter_slots,
        )

    def decide_after_game(
        self,
        completed_game: ReplayPlayerGame,
        *,
        remaining_games: tuple[ReplayPlayerGame, ...],
        open_slots: tuple[tuple[int, str], ...],
        locked_slots: tuple[LockedSlot, ...],
        decision_time: datetime,
        league_id: str,
        week: int,
        roster_id: int,
        run_seed: int | None = None,
        scenario_count: int | None = None,
    ) -> PolicyDecision:
        if completed_game.projection is None:
            raise ValueError("Completed game requires a point-in-time projection")
        if not completed_game.rostered_at_tipoff:
            return PolicyDecision(
                "pass",
                completed_game.sleeper_id,
                completed_game.game_id,
                None,
                sum(slot.score for slot in locked_slots),
                0.0,
                completed_game.projection.input_version,
                "PASS because the player was not rostered at tipoff.",
            )
        legal_open_slots = tuple(
            (slot_index, position)
            for slot_index, position in open_slots
            if eligible_for_slot(completed_game.eligible_positions, position)
        )
        slots = tuple(position for _, position in legal_open_slots)
        locked_score = sum(slot.score for slot in locked_slots)
        if not slots:
            return PolicyDecision(
                "pass",
                completed_game.sleeper_id,
                completed_game.game_id,
                None,
                locked_score,
                0.0,
                completed_game.projection.input_version,
                "No legal open starting slot remained.",
            )
        count = scenario_count or self.config.scenario_count
        seed = stable_scenario_seed(
            self.config.seed if run_seed is None else run_seed,
            league_id=league_id,
            week=week,
            roster_id=roster_id,
            decision_time=decision_time,
        )
        scenarios = generate_scenarios(
            remaining_games,
            decision_time=decision_time,
            count=count,
            seed=seed,
        )
        pass_value = locked_score + rollout_terminal_score(
            fixed_assignments=(),
            remaining_games=remaining_games,
            open_slots=slots,
            scenarios=scenarios,
        )
        best_lock: tuple[float, int, float] | None = None
        for slot_offset, (slot_index, _) in enumerate(legal_open_slots):
            fixed = AssignmentCandidate(
                candidate_id=f"locked:{completed_game.sleeper_id}:{completed_game.game_id}",
                player_id=completed_game.sleeper_id,
                score=completed_game.actual_score,
                eligible_positions=completed_game.eligible_positions,
                game_id=completed_game.game_id,
            )
            remaining_slots = slots[:slot_offset] + slots[slot_offset + 1 :]
            lock_value = locked_score + rollout_terminal_score(
                fixed_assignments=(fixed,),
                remaining_games=remaining_games,
                open_slots=remaining_slots,
                scenarios=scenarios,
            )
            candidate = (lock_value, slot_index, lock_value - pass_value)
            if best_lock is None or candidate[0] > best_lock[0] + 1e-9:
                best_lock = candidate
        assert best_lock is not None
        if best_lock[0] <= pass_value + self.config.tie_tolerance:
            return PolicyDecision(
                "pass",
                completed_game.sleeper_id,
                completed_game.game_id,
                None,
                pass_value,
                pass_value - best_lock[0],
                completed_game.projection.input_version,
                "PASS preserved future own-team slot flexibility within tie tolerance.",
            )
        return PolicyDecision(
            "lock",
            completed_game.sleeper_id,
            completed_game.game_id,
            best_lock[1],
            best_lock[0],
            best_lock[0] - pass_value,
            completed_game.projection.input_version,
            "LOCK maximized expected terminal own-team score across deterministic scenarios.",
        )


__all__ = (
    "LockInContext",
    "LockInPolicy",
    "LockInPolicyConfig",
    "PolicyDecision",
    "ScoreMaximizingLockInPolicy",
)
