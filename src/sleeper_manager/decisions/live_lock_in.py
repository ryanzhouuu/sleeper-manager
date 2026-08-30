"""Translate stable live evidence into actionable or deferred Lock-In outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from sleeper_manager.decisions.lock_in import (
    LockInPolicyError,
    ScoreMaximizingLockInPolicy,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import (
    LockInEvaluation,
    LockInEvaluationKind,
)
from sleeper_manager.domain.planning import (
    GameOpportunity,
    PlanningGameStatus,
    TeamWeekState,
)


@dataclass(frozen=True, slots=True)
class LiveLockInPolicyConfig:
    """Control the minimum scenario confidence required for live advice."""

    minimum_confidence: float

    def __post_init__(self) -> None:
        """Reject confidence thresholds outside the probability interval."""

        if not isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("Minimum Lock-In confidence must be between zero and one")


def evaluate_live_lock_in(
    state: TeamWeekState,
    completed_game: GameOpportunity,
    *,
    deadline: datetime,
    manager_policy_version: str,
    policy: ScoreMaximizingLockInPolicy,
    config: LiveLockInPolicyConfig,
) -> LockInEvaluation:
    """Evaluate stable final evidence using the shared historical policy."""

    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("Lock-In deadlines must be timezone-aware")
    trace = _evaluation_trace(state, completed_game, config)
    if state.decision_time >= deadline:
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.UNAVAILABLE,
            reason="deadline_elapsed",
            trace=trace,
        )
    if state.is_blocked:
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.UNAVAILABLE,
            reason="planning_state_blocked",
            trace=trace,
        )
    if not _has_legal_open_slot(state, completed_game):
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.UNAVAILABLE,
            reason="ineligible_at_tipoff",
            trace=trace,
        )
    if not _has_later_eligible_game(state, completed_game):
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.AUTOMATIC_FINAL,
            reason="final_eligible_game",
            trace=trace,
        )
    try:
        comparison = policy.compare_after_game(state, completed_game)
    except LockInPolicyError as error:
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.UNAVAILABLE,
            reason=_policy_error_reason(error),
            trace=trace,
        )
    if not comparison.selected_terminal_scores:
        return _deferred_evaluation(
            state,
            completed_game,
            deadline=deadline,
            manager_policy_version=manager_policy_version,
            kind=LockInEvaluationKind.UNAVAILABLE,
            reason="scenario_comparison_unavailable",
            trace=trace,
        )
    confidence = _scenario_confidence(
        comparison.selected_terminal_scores,
        comparison.counterfactual_terminal_scores,
        tie_tolerance=policy.config.tie_tolerance,
    )
    alternative_scores = comparison.counterfactual_terminal_scores
    if confidence < config.minimum_confidence:
        return LockInEvaluation(
            decision_time=state.decision_time,
            kind=LockInEvaluationKind.WAIT,
            player_id=completed_game.sleeper_player_id,
            game_id=completed_game.game_id,
            deadline=deadline,
            information_version=state.input_version,
            manager_policy_version=manager_policy_version,
            reason_codes=("confidence_below_threshold",),
            trace=trace,
            observed_score=completed_game.completed_fantasy_score,
            alternative_expected_score=_mean(alternative_scores),
            alternative_percentiles=_percentiles(alternative_scores),
            confidence=confidence,
        )
    return LockInEvaluation(
        decision_time=state.decision_time,
        kind=LockInEvaluationKind(comparison.decision.kind.value),
        player_id=completed_game.sleeper_player_id,
        game_id=completed_game.game_id,
        deadline=deadline,
        information_version=state.input_version,
        manager_policy_version=manager_policy_version,
        reason_codes=("confidence_met",),
        trace=trace,
        observed_score=completed_game.completed_fantasy_score,
        alternative_expected_score=_mean(alternative_scores),
        alternative_percentiles=_percentiles(alternative_scores),
        confidence=confidence,
        decision=comparison.decision,
    )


def _deferred_evaluation(
    state: TeamWeekState,
    completed_game: GameOpportunity,
    *,
    deadline: datetime,
    manager_policy_version: str,
    kind: LockInEvaluationKind,
    reason: str,
    trace: tuple[tuple[str, str], ...],
) -> LockInEvaluation:
    """Build a non-actionable evaluation while retaining observed evidence."""

    return LockInEvaluation(
        decision_time=state.decision_time,
        kind=kind,
        player_id=completed_game.sleeper_player_id,
        game_id=completed_game.game_id,
        deadline=deadline,
        information_version=state.input_version,
        manager_policy_version=manager_policy_version,
        reason_codes=(reason,),
        trace=trace,
        observed_score=completed_game.completed_fantasy_score,
    )


def _has_legal_open_slot(state: TeamWeekState, opportunity: GameOpportunity) -> bool:
    """Report whether the tipoff evidence admits a current starting slot."""

    return opportunity.rostered_at_tipoff is True and any(
        slot.index in state.open_slot_indices
        and slot.index in opportunity.eligible_slot_indices
        and eligible_for_slot(opportunity.eligible_positions, slot.position)
        for slot in state.starter_slots
    )


def _has_later_eligible_game(state: TeamWeekState, completed_game: GameOpportunity) -> bool:
    """Report whether this player retains a legal later chance in the fantasy week."""

    open_indices = set(state.open_slot_indices)
    return any(
        opportunity.sleeper_player_id == completed_game.sleeper_player_id
        and opportunity.game_id != completed_game.game_id
        and opportunity.status is PlanningGameStatus.SCHEDULED
        and opportunity.scheduled_start > state.decision_time
        and bool(open_indices.intersection(opportunity.eligible_slot_indices))
        for opportunity in state.opportunities
    )


def _scenario_confidence(
    selected: tuple[float, ...],
    counterfactual: tuple[float, ...],
    *,
    tie_tolerance: float,
) -> float:
    """Return the fraction of scenarios with a material selected-action win."""

    wins = sum(
        selected_score - counterfactual_score > tie_tolerance
        for selected_score, counterfactual_score in zip(selected, counterfactual, strict=True)
    )
    return wins / len(selected)


def _mean(values: tuple[float, ...]) -> float:
    """Return a canonical six-decimal scenario mean."""

    return round(sum(values) / len(values), 6)


def _percentiles(values: tuple[float, ...]) -> tuple[tuple[int, float], ...]:
    """Summarize the alternative terminal distribution at stable percentile ranks."""

    ordered = tuple(sorted(values))
    return tuple((rank, ordered[round((len(ordered) - 1) * rank / 100)]) for rank in (10, 50, 90))


def _evaluation_trace(
    state: TeamWeekState,
    completed_game: GameOpportunity,
    config: LiveLockInPolicyConfig,
) -> tuple[tuple[str, str], ...]:
    """Capture stable versions and thresholds that explain the live result."""

    return (
        ("league_configuration_version", state.league_configuration_version),
        ("scoring_policy_version", state.scoring_policy_version),
        ("projection_model_version", state.projection_model_version),
        ("completed_game_status", completed_game.status.value),
        ("minimum_confidence", f"{config.minimum_confidence:.6f}"),
    )


def _policy_error_reason(error: LockInPolicyError) -> str:
    """Reduce a policy failure to a stable non-actionable reason code."""

    message = str(error).lower()
    if "projection" in message:
        return "projection_unavailable"
    if "blocked" in message:
        return "planning_state_blocked"
    return "policy_input_unavailable"


__all__ = (
    "LiveLockInPolicyConfig",
    "evaluate_live_lock_in",
)
