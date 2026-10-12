"""Score-maximizing Lock-In policy over validated team-week planning state.

The policy consumes production-neutral domain records. Historical replay and
live collection adapt their evidence into the same ``TeamWeekState`` boundary
before invoking this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from sleeper_manager.decisions._lock_in_continuations import lock_in_continuations
from sleeper_manager.decisions._lock_in_placements import feasible_lock_slot_indices
from sleeper_manager.decisions.simulation import (
    ScenarioInput,
    generate_projection_scenarios,
    stable_scenario_seed,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInDecision, LockInDecisionKind
from sleeper_manager.domain.planning import (
    GameOpportunity,
    PlanningGameStatus,
    TeamWeekState,
)


class LockInPolicyError(ValueError):
    """Report that planning state cannot support a safe Lock-In decision."""


@dataclass(frozen=True, slots=True)
class LockInPolicyConfig:
    """Control deterministic scenario sampling and policy tie handling."""

    scenario_count: int = 2000
    seed: int = 0
    tie_tolerance: float = 0.01

    def __post_init__(self) -> None:
        """Reject configurations that cannot produce a meaningful rollout."""

        if self.scenario_count <= 0:
            raise ValueError("Scenario count must be positive")
        if self.tie_tolerance < 0:
            raise ValueError("Tie tolerance must be non-negative")


@dataclass(frozen=True, slots=True)
class LockInComparison:
    """Expose the selected and counterfactual terminal values by scenario."""

    decision: LockInDecision
    selected_terminal_scores: tuple[float, ...]
    counterfactual_terminal_scores: tuple[float, ...]

    def __post_init__(self) -> None:
        """Require paired scenarios whenever a comparison was available."""

        if len(self.selected_terminal_scores) != len(self.counterfactual_terminal_scores):
            raise ValueError("Lock-In comparison scenarios must be paired")


class ScoreMaximizingLockInPolicy:
    """Choose Lock or Pass by maximizing expected own-team terminal score."""

    def __init__(self, config: LockInPolicyConfig | None = None) -> None:
        """Use explicit policy configuration or deterministic defaults."""

        self.config = config or LockInPolicyConfig()

    def decide_after_game(
        self,
        state: TeamWeekState,
        completed_game: GameOpportunity,
    ) -> LockInDecision:
        """Evaluate one finalized opportunity against every legal future alternative."""

        return self.compare_after_game(state, completed_game).decision

    def compare_after_game(
        self,
        state: TeamWeekState,
        completed_game: GameOpportunity,
    ) -> LockInComparison:
        """Return the historical decision plus paired scenario terminal values."""

        _validate_policy_input(state, completed_game)
        completed_score = completed_game.completed_fantasy_score
        assert completed_score is not None
        locked_score = sum(slot.accepted_fantasy_score for slot in state.fixed_slots)
        if completed_game.rostered_at_tipoff is False:
            return LockInComparison(
                decision=_terminal_pass(
                    state,
                    completed_game,
                    expected_terminal_score=locked_score,
                    reason="PASS because the player was not rostered at tipoff.",
                ),
                selected_terminal_scores=(),
                counterfactual_terminal_scores=(),
            )
        if completed_game.rostered_at_tipoff is None:
            raise LockInPolicyError("Completed opportunity lacks tipoff roster evidence")

        open_slots = tuple(
            slot for slot in state.starter_slots if slot.index in state.open_slot_indices
        )
        feasible_indices = feasible_lock_slot_indices(state, completed_game)
        legal_open_slots = tuple(
            slot
            for slot in open_slots
            if slot.index in completed_game.eligible_slot_indices
            and eligible_for_slot(completed_game.eligible_positions, slot.position)
            and slot.index in feasible_indices
        )
        if not legal_open_slots:
            return LockInComparison(
                decision=_terminal_pass(
                    state,
                    completed_game,
                    expected_terminal_score=locked_score,
                    reason="No legal open starting slot remained.",
                ),
                selected_terminal_scores=(),
                counterfactual_terminal_scores=(),
            )

        remaining = decision_critical_opportunities(state, completed_game)
        inputs = tuple(_scenario_input(opportunity) for opportunity in remaining)
        seed = stable_scenario_seed(
            self.config.seed,
            league_id=state.league_id,
            week=state.week,
            roster_id=state.roster_id,
            decision_time=state.decision_time,
        )
        scenarios = generate_projection_scenarios(
            inputs,
            decision_time=state.decision_time,
            count=self.config.scenario_count,
            seed=seed,
        )
        continuations = lock_in_continuations(
            inputs,
            open_slots,
            scenarios=scenarios,
            player_id=completed_game.sleeper_player_id,
            completed_score=completed_score,
            legal_slot_indices=tuple(slot.index for slot in legal_open_slots),
            locked_score=locked_score,
        )
        pass_value, pass_scores = continuations.pass_value, continuations.pass_scores
        best_lock: tuple[float, int, float, tuple[float, ...]] | None = None
        for slot_index, lock_value, lock_scores in continuations.lock_options:
            candidate = (lock_value, slot_index, lock_value - pass_value, lock_scores)
            if best_lock is None or candidate[0] > best_lock[0] + 1e-9:
                best_lock = candidate

        assert best_lock is not None
        if best_lock[0] <= pass_value + self.config.tie_tolerance:
            return LockInComparison(
                decision=LockInDecision(
                    decision_time=state.decision_time,
                    kind=LockInDecisionKind.PASS,
                    player_id=completed_game.sleeper_player_id,
                    game_id=completed_game.game_id,
                    slot_index=None,
                    expected_terminal_score=pass_value,
                    counterfactual_value=pass_value - best_lock[0],
                    information_version=state.input_version,
                    reason="PASS preserved future own-team slot flexibility within tie tolerance.",
                ),
                selected_terminal_scores=pass_scores,
                counterfactual_terminal_scores=best_lock[3],
            )
        return LockInComparison(
            decision=LockInDecision(
                decision_time=state.decision_time,
                kind=LockInDecisionKind.LOCK,
                player_id=completed_game.sleeper_player_id,
                game_id=completed_game.game_id,
                slot_index=best_lock[1],
                expected_terminal_score=best_lock[0],
                counterfactual_value=best_lock[0] - pass_value,
                information_version=state.input_version,
                reason=(
                    "LOCK maximized expected terminal own-team score "
                    "across deterministic scenarios."
                ),
            ),
            selected_terminal_scores=best_lock[3],
            counterfactual_terminal_scores=pass_scores,
        )


def decision_critical_opportunities(
    state: TeamWeekState,
    completed_game: GameOpportunity,
) -> tuple[GameOpportunity, ...]:
    """Return every legal future opportunity that can affect the current decision."""

    locked_players = {slot.player_id for slot in state.fixed_slots}
    passed = {(item.player_id, item.game_id) for item in state.passed_opportunities}
    open_indices = set(state.open_slot_indices)
    remaining = (
        opportunity
        for opportunity in state.opportunities
        if opportunity.sleeper_player_id not in locked_players
        if (opportunity.sleeper_player_id, opportunity.game_id)
        != (completed_game.sleeper_player_id, completed_game.game_id)
        if (opportunity.sleeper_player_id, opportunity.game_id) not in passed
        if opportunity.rostered_at_tipoff is True
        if opportunity.status is PlanningGameStatus.SCHEDULED
        if opportunity.scheduled_start > state.decision_time
        if open_indices.intersection(opportunity.eligible_slot_indices)
    )
    return tuple(
        sorted(
            remaining,
            key=lambda item: (
                item.sleeper_player_id,
                item.scheduled_start,
                item.game_id,
            ),
        )
    )


def missing_decision_projection_keys(
    opportunities: tuple[GameOpportunity, ...],
) -> tuple[str, ...]:
    """Identify decision-critical opportunities without an as-of projection."""

    return tuple(
        _opportunity_key(opportunity)
        for opportunity in opportunities
        if opportunity.projection is None
    )


def _validate_policy_input(state: TeamWeekState, completed_game: GameOpportunity) -> None:
    """Require a visible finalized candidate and otherwise decision-ready state."""

    if state.is_blocked:
        reasons = ", ".join(reason.value for reason in state.blocking_reasons)
        raise LockInPolicyError(f"Lock-In planning state is blocked: {reasons}")
    if completed_game not in state.opportunities:
        raise LockInPolicyError("Completed opportunity is not part of the team-week state")
    if (
        completed_game.status is not PlanningGameStatus.FINAL
        or completed_game.finalized_at is None
        or completed_game.completed_fantasy_score is None
    ):
        raise LockInPolicyError("Lock-In candidates require a finalized completed score")


def _scenario_input(opportunity: GameOpportunity) -> ScenarioInput:
    """Normalize one projected domain opportunity for deterministic rollout."""

    if opportunity.projection is None:
        raise LockInPolicyError(f"Missing projection for {_opportunity_key(opportunity)}")
    return ScenarioInput(
        candidate_id=_opportunity_key(opportunity),
        player_id=opportunity.sleeper_player_id,
        game_id=opportunity.game_id,
        eligible_positions=opportunity.eligible_positions,
        projection=opportunity.projection,
        eligible_slot_indices=opportunity.eligible_slot_indices,
    )


def _terminal_pass(
    state: TeamWeekState,
    completed_game: GameOpportunity,
    *,
    expected_terminal_score: float,
    reason: str,
) -> LockInDecision:
    """Build a Pass when no scenario comparison is legally available or required."""

    return LockInDecision(
        decision_time=state.decision_time,
        kind=LockInDecisionKind.PASS,
        player_id=completed_game.sleeper_player_id,
        game_id=completed_game.game_id,
        slot_index=None,
        expected_terminal_score=expected_terminal_score,
        counterfactual_value=0.0,
        information_version=state.input_version,
        reason=reason,
    )


def _opportunity_key(opportunity: GameOpportunity) -> str:
    """Return the stable player-game identity shared by policy and diagnostics."""

    return f"{opportunity.sleeper_player_id}:{opportunity.game_id}"


__all__ = (
    "LockInComparison",
    "LockInPolicyConfig",
    "LockInPolicyError",
    "ScoreMaximizingLockInPolicy",
    "decision_critical_opportunities",
    "missing_decision_projection_keys",
)
