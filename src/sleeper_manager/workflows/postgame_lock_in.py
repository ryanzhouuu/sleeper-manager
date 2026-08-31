"""Observe direct ESPN finals and turn stable scores into live Lock-In advice."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol

from sleeper_manager.decisions.live_lock_in import (
    LiveLockInPolicyConfig,
    evaluate_live_lock_in,
)
from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy
from sleeper_manager.domain.lock_in import (
    LockInEvaluation,
    LockInEvaluationKind,
    LockInOpportunityStatus,
)
from sleeper_manager.domain.nba import DataQualityState, GameStatus, GameSummary, ProviderResult
from sleeper_manager.domain.planning import GameOpportunity, PlanningGameStatus, TeamWeekState
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import calculate_fantasy_points
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, RecommendationRecord
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInObservation,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.lock_in_evidence import merge_lock_in_opportunity_evidence
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    RecommendationRequest,
    recommendation_id_for,
)
from sleeper_manager.workflows.planning_inputs import (
    LivePlanningInputs,
    build_live_team_week_state,
)

LOCK_IN_DECISION_TYPE = "live_lock_in"
LOCK_IN_WARNING_TYPE = "live_lock_in_unavailable"
LOCK_IN_ACKNOWLEDGEMENT_KINDS = frozenset({LOCK_IN_DECISION_TYPE, "placeholder_lock_in"})
_RECHECK_DELAY = timedelta(minutes=5)


class DirectGameSummarySource(Protocol):
    """Fetch an uncached provider summary for one scheduled postgame wake."""

    async def __call__(self, game_id: str) -> ProviderResult[GameSummary]: ...


@dataclass(frozen=True, slots=True)
class PostgameLockInResult:
    """Report whether a postgame wake should complete or remain scheduled."""

    outcome: Literal[
        "wait",
        "notified",
        "duplicate",
        "unavailable",
        "automatic_final",
        "delivery_failed",
    ]
    recommendation: RecommendationRecord | None = None
    evaluation: LockInEvaluation | None = None


async def run_postgame_lock_in(
    game_id: str,
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    fetch_summary: DirectGameSummarySource,
    runtime_policy: RuntimePolicy,
    open_sleeper_url: str,
    poll_id: str,
    player_names: dict[str, str] | None = None,
    policy: ScoreMaximizingLockInPolicy | None = None,
) -> PostgameLockInResult:
    """Fetch once, stabilize every relevant player, evaluate, and notify safely."""

    due = await repository.list_due_lock_in_opportunities(decision_time)
    opportunities = tuple(item for item in due if item.key.game_id == game_id)
    if not opportunities:
        return PostgameLockInResult("wait")
    summary_result = await fetch_summary(game_id)
    summary = summary_result.records
    reason = summary_wait_reason(summary_result, opportunities)
    if reason is not None:
        await _defer_opportunities(
            repository,
            opportunities,
            decision_time=decision_time,
            reason=reason,
            active=summary.game.status is GameStatus.IN_PROGRESS,
        )
        return PostgameLockInResult("wait")

    fingerprint = summary_fingerprint(summary)
    boxes = {item.player_id: item for item in summary.player_box_scores}
    updated: list[LockInOpportunityRecord] = []
    for opportunity in opportunities:
        box_score = boxes[opportunity.provider_player_id]
        score = calculate_fantasy_points(box_score.line, inputs.league_profile.scoring)
        changed = (
            opportunity.current_observation_fingerprint is not None
            and opportunity.current_observation_fingerprint != fingerprint
        )
        if changed and opportunity.current_recommendation_id is not None:
            await repository.supersede_recommendation(
                opportunity.current_recommendation_id,
                decision_time,
            )
        stored = await repository.record_lock_in_observation(
            opportunity.key,
            LockInObservation(
                score=score,
                fingerprint=fingerprint,
                poll_id=poll_id,
                observed_at=decision_time,
                next_check_at=decision_time + _RECHECK_DELAY,
            ),
            expected_version=opportunity.row_version,
        )
        if stored is not None:
            updated.append(stored)

    stable = tuple(
        item
        for item in updated
        if item.stable_fingerprint == fingerprint and item.consecutive_direct_poll_count >= 2
    )
    if not stable:
        return PostgameLockInResult("wait")

    live_inputs = await _inputs_with_lock_in_evidence(inputs, repository)
    state = build_live_team_week_state(live_inputs, decision_time=decision_time)
    results: list[PostgameLockInResult] = []
    for opportunity in stable:
        results.append(
            await _evaluate_opportunity(
                opportunity,
                state,
                summary=summary,
                repository=repository,
                notifications=notifications,
                runtime_policy=runtime_policy,
                open_sleeper_url=open_sleeper_url,
                player_names=player_names or {},
                policy=policy or ScoreMaximizingLockInPolicy(),
            )
        )
    return _combined_result(results)


def summary_wait_reason(
    result: ProviderResult[GameSummary],
    opportunities: tuple[LockInOpportunityRecord, ...],
) -> str | None:
    """Identify provider evidence that cannot count as a stable final poll."""

    if result.records.game.status is not GameStatus.FINAL:
        return "game_not_final"
    if result.quality.state in {
        DataQualityState.PARTIAL,
        DataQualityState.EMPTY,
        DataQualityState.ERROR,
        DataQualityState.UNRESOLVED,
        DataQualityState.STALE,
    }:
        return "summary_incomplete"
    present = {item.player_id for item in result.records.player_box_scores}
    if any(item.provider_player_id not in present for item in opportunities):
        return "rostered_player_missing"
    return None


async def _defer_opportunities(
    repository: AsyncRuntimeStateRepository,
    opportunities: tuple[LockInOpportunityRecord, ...],
    *,
    decision_time: datetime,
    reason: str,
    active: bool,
) -> None:
    """Persist Wait evidence without emitting a routine notification."""

    for opportunity in opportunities:
        deferred = replace(
            opportunity,
            status=(
                LockInOpportunityStatus.ACTIVE if active else LockInOpportunityStatus.FINALIZING
            ),
            next_check_at=decision_time + _RECHECK_DELAY,
            trace_json=json.dumps({"reason_codes": [reason]}, sort_keys=True),
            updated_at=decision_time,
        )
        await repository.update_lock_in_opportunity(
            deferred,
            expected_version=opportunity.row_version,
        )


async def _evaluate_opportunity(
    opportunity: LockInOpportunityRecord,
    state: TeamWeekState,
    *,
    summary: GameSummary,
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    runtime_policy: RuntimePolicy,
    open_sleeper_url: str,
    player_names: dict[str, str],
    policy: ScoreMaximizingLockInPolicy,
) -> PostgameLockInResult:
    """Evaluate one stable player score and persist its material outcome."""

    completed_state = _completed_state(state, opportunity, summary)
    if completed_state is None:
        evaluation = _mapping_unavailable_evaluation(state, opportunity, runtime_policy)
    else:
        completed, live_state = completed_state
        evaluation = evaluate_live_lock_in(
            live_state,
            completed,
            deadline=opportunity.action_deadline,
            manager_policy_version=runtime_policy.manager_intent.version,
            policy=policy,
            config=LiveLockInPolicyConfig(runtime_policy.manager_intent.minimum_confidence),
        )
    evaluation_hash = _evaluation_hash(evaluation)
    trace_json = _evaluation_trace_json(evaluation, opportunity)
    if evaluation.kind is LockInEvaluationKind.WAIT:
        await _supersede_changed_recommendation(
            repository,
            opportunity,
            replacement_id=None,
            changed_at=state.decision_time,
        )
        await _store_evaluation(
            repository,
            opportunity,
            status=LockInOpportunityStatus.FINALIZING,
            evaluation_hash=evaluation_hash,
            trace_json=trace_json,
        )
        return PostgameLockInResult("wait", evaluation=evaluation)
    if evaluation.kind is LockInEvaluationKind.AUTOMATIC_FINAL:
        await _supersede_changed_recommendation(
            repository,
            opportunity,
            replacement_id=None,
            changed_at=state.decision_time,
        )
        await _store_evaluation(
            repository,
            opportunity,
            status=LockInOpportunityStatus.AUTOMATIC_FINAL,
            evaluation_hash=evaluation_hash,
            trace_json=trace_json,
        )
        return PostgameLockInResult("automatic_final", evaluation=evaluation)

    decision_type = (
        LOCK_IN_WARNING_TYPE
        if evaluation.kind is LockInEvaluationKind.UNAVAILABLE
        else LOCK_IN_DECISION_TYPE
    )
    idempotency_key = _material_idempotency_key(opportunity, evaluation, evaluation_hash)
    await _supersede_changed_recommendation(
        repository,
        opportunity,
        replacement_id=recommendation_id_for(idempotency_key),
        changed_at=state.decision_time,
    )
    request = RecommendationRequest(
        league_id=opportunity.key.league_id,
        fantasy_week=opportunity.key.fantasy_week,
        player_id=opportunity.key.player_id,
        game_id=opportunity.key.game_id,
        decision_type=decision_type,
        title=_evaluation_title(evaluation, player_names),
        message=_evaluation_message(evaluation),
        deadline=opportunity.action_deadline,
        policy_version=runtime_policy.manager_intent.version,
        open_sleeper_url=open_sleeper_url,
        trace_json=trace_json,
        idempotency_key=idempotency_key,
    )
    result = await notifications.run(request)
    stored = await repository.get_lock_in_opportunity(opportunity.key)
    if stored is not None:
        await _store_evaluation(
            repository,
            stored,
            status=(
                LockInOpportunityStatus.RECONCILIATION_REQUIRED
                if evaluation.kind is LockInEvaluationKind.UNAVAILABLE
                else LockInOpportunityStatus.ACTIONABLE
            ),
            evaluation_hash=evaluation_hash,
            trace_json=trace_json,
            recommendation=result.recommendation,
        )
    outcome: Literal["notified", "duplicate", "unavailable", "delivery_failed"]
    if result.status == "delivery_failed":
        outcome = "delivery_failed"
    elif evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
        outcome = "unavailable"
    else:
        outcome = "duplicate" if result.status == "duplicate" else "notified"
    return PostgameLockInResult(outcome, result.recommendation, evaluation)


async def _supersede_changed_recommendation(
    repository: AsyncRuntimeStateRepository,
    opportunity: LockInOpportunityRecord,
    *,
    replacement_id: str | None,
    changed_at: datetime,
) -> None:
    """Retire stale pending advice before storing a materially different outcome."""

    current_id = opportunity.current_recommendation_id
    if current_id is not None and current_id != replacement_id:
        await repository.supersede_recommendation(current_id, changed_at)


def _completed_state(
    state: TeamWeekState,
    opportunity: LockInOpportunityRecord,
    summary: GameSummary,
) -> tuple[GameOpportunity, TeamWeekState] | None:
    """Overlay durable final evidence when the current mapped player-game still exists."""

    target = next(
        (
            item
            for item in state.opportunities
            if item.sleeper_player_id == opportunity.key.player_id
            and item.game_id == opportunity.key.game_id
        ),
        None,
    )
    if target is None:
        return None
    completed = replace(
        target,
        status=PlanningGameStatus.FINAL,
        eligible_slot_indices=(
            (opportunity.slot_index,) if opportunity.slot_index is not None else ()
        ),
        eligible_positions=opportunity.eligible_positions,
        rostered_at_tipoff=opportunity.rostered_at_tipoff,
        completed_fantasy_score=opportunity.stable_score,
        finalized_at=summary.game.finalized_at or state.decision_time,
        data_quality="stable-direct-espn-summary",
    )
    finalized_at = completed.finalized_at
    return completed, replace(
        state,
        opportunities=tuple(
            completed
            if item == target
            else replace(item, status=PlanningGameStatus.FINAL, finalized_at=finalized_at)
            if item.game_id == opportunity.key.game_id
            else item
            for item in state.opportunities
        ),
    )


def _mapping_unavailable_evaluation(
    state: TeamWeekState,
    opportunity: LockInOpportunityRecord,
    runtime_policy: RuntimePolicy,
) -> LockInEvaluation:
    """Describe a persisted player-game that disappeared from current mapped inputs."""

    return LockInEvaluation(
        decision_time=state.decision_time,
        kind=LockInEvaluationKind.UNAVAILABLE,
        player_id=opportunity.key.player_id,
        game_id=opportunity.key.game_id,
        deadline=opportunity.action_deadline,
        information_version=state.input_version,
        manager_policy_version=runtime_policy.manager_intent.version,
        reason_codes=("mapping_unavailable",),
        trace=(
            ("league_configuration_version", state.league_configuration_version),
            ("scoring_policy_version", state.scoring_policy_version),
            ("projection_model_version", state.projection_model_version),
            ("current_player_game_mapping", "missing"),
        ),
        observed_score=opportunity.stable_score,
    )


async def _store_evaluation(
    repository: AsyncRuntimeStateRepository,
    opportunity: LockInOpportunityRecord,
    *,
    status: LockInOpportunityStatus,
    evaluation_hash: str,
    trace_json: str,
    recommendation: RecommendationRecord | None = None,
) -> None:
    """Guard the latest evaluation and recommendation identity against stale writers."""

    updated = replace(
        opportunity,
        status=status,
        current_recommendation_id=(recommendation.recommendation_id if recommendation else None),
        current_recommendation_kind=(recommendation.decision_type if recommendation else None),
        latest_evaluation_hash=evaluation_hash,
        trace_json=trace_json,
        updated_at=opportunity.current_observed_at or opportunity.updated_at,
    )
    await repository.update_lock_in_opportunity(
        updated,
        expected_version=opportunity.row_version,
    )


def summary_fingerprint(summary: GameSummary) -> str:
    """Hash normalized game and box-score facts without retrieval timestamps."""

    payload = {
        "game": {
            "id": summary.game.provider_id,
            "start": summary.game.start_time.isoformat(),
            "status": summary.game.status.value,
            "completed_periods": summary.game.completed_periods,
        },
        "players": [
            {
                "player_id": item.player_id,
                "team_id": item.team_id,
                "started": item.started,
                "did_play": item.did_play,
                "minutes": item.minutes,
                "line": asdict(item.line),
            }
            for item in sorted(summary.player_box_scores, key=lambda value: value.player_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _evaluation_hash(evaluation: LockInEvaluation) -> str:
    """Hash canonical rounded material advice while excluding retrieval timestamps."""

    payload = {
        "kind": evaluation.kind.value,
        "score": _rounded(evaluation.observed_score),
        "alternative": _rounded(evaluation.alternative_expected_score),
        "percentiles": [
            [rank, _rounded(value)] for rank, value in evaluation.alternative_percentiles
        ],
        "confidence": _rounded(evaluation.confidence),
        "reason_codes": evaluation.reason_codes,
        "trace": evaluation.trace,
        "slot_index": evaluation.decision.slot_index if evaluation.decision else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _rounded(value: float | None) -> float | None:
    """Canonicalize material numeric evidence to two fantasy-score decimals."""

    return round(value, 2) if value is not None else None


def _evaluation_trace_json(
    evaluation: LockInEvaluation,
    opportunity: LockInOpportunityRecord,
) -> str:
    """Serialize the auditable live evaluation trace for persistence and acknowledgements."""

    payload: dict[str, object] = {
        "kind": evaluation.kind.value,
        "reason_codes": evaluation.reason_codes,
        "trace": dict(evaluation.trace),
        "observed_score": evaluation.observed_score,
        "alternative_expected_score": evaluation.alternative_expected_score,
        "alternative_percentiles": evaluation.alternative_percentiles,
        "confidence": evaluation.confidence,
    }
    if evaluation.kind is LockInEvaluationKind.LOCK:
        payload["acknowledgement"] = {
            "schema_version": 1,
            "slot_index": evaluation.decision.slot_index if evaluation.decision else None,
            "slot_position": opportunity.slot_position,
            "accepted_fantasy_score": evaluation.observed_score,
        }
    elif evaluation.kind is LockInEvaluationKind.PASS:
        payload["acknowledgement"] = {"schema_version": 1}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _material_idempotency_key(
    opportunity: LockInOpportunityRecord,
    evaluation: LockInEvaluation,
    evaluation_hash: str,
) -> str:
    """Identify one material player-game recommendation across retries and restarts."""

    return ":".join(
        (
            opportunity.key.league_id,
            str(opportunity.key.fantasy_week),
            str(opportunity.key.roster_id),
            opportunity.key.player_id,
            opportunity.key.game_id,
            evaluation.manager_policy_version,
            str(opportunity.score_revision),
            evaluation.kind.value,
            evaluation.deadline.isoformat(),
            evaluation_hash,
        )
    )


def _evaluation_title(
    evaluation: LockInEvaluation,
    player_names: dict[str, str],
) -> str:
    """Render a compact actionable or warning title."""

    player = player_names.get(evaluation.player_id, evaluation.player_id)
    if evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
        return f"Lock-In unavailable: {player}"
    return f"{evaluation.kind.value.title()} {player}?"


def _evaluation_message(evaluation: LockInEvaluation) -> str:
    """Render stable score, alternative value, confidence, and reason evidence."""

    if evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
        return "Safe Lock-In advice is unavailable: " + ", ".join(evaluation.reason_codes)
    assert evaluation.observed_score is not None
    assert evaluation.alternative_expected_score is not None
    assert evaluation.confidence is not None
    return (
        f"Observed {evaluation.observed_score:.2f}; alternative expected "
        f"{evaluation.alternative_expected_score:.2f}; confidence "
        f"{evaluation.confidence:.0%}. Complete the action in Sleeper, then acknowledge it."
    )


def _combined_result(results: list[PostgameLockInResult]) -> PostgameLockInResult:
    """Choose the most operationally significant result from a coalesced game."""

    for outcome in (
        "delivery_failed",
        "notified",
        "unavailable",
        "duplicate",
        "wait",
        "automatic_final",
    ):
        match = next((item for item in results if item.outcome == outcome), None)
        if match is not None:
            return match
    return PostgameLockInResult("wait")


async def _inputs_with_lock_in_evidence(
    inputs: LivePlanningInputs,
    repository: AsyncRuntimeStateRepository,
) -> LivePlanningInputs:
    """Overlay current locked, passed, and automatic-final scores onto live inputs."""

    records = await repository.load_acknowledged_lock_in_opportunities(
        inputs.league_profile.league_id,
        inputs.week_window.week,
    )
    return replace(
        inputs,
        acknowledgements=merge_lock_in_opportunity_evidence(inputs.acknowledgements, records),
    )


__all__ = (
    "DirectGameSummarySource",
    "LOCK_IN_ACKNOWLEDGEMENT_KINDS",
    "LOCK_IN_DECISION_TYPE",
    "LOCK_IN_WARNING_TYPE",
    "PostgameLockInResult",
    "run_postgame_lock_in",
    "summary_fingerprint",
    "summary_wait_reason",
)
