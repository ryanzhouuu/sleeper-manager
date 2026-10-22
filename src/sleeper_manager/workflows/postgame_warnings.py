"""Blocked score advice includes the full observed team view without actionable confirmations."""

import json
from dataclasses import replace
from zoneinfo import ZoneInfo

from sleeper_manager.domain.lock_in import (
    LockInEvaluation,
    LockInEvaluationKind,
    LockInOpportunityStatus,
)
from sleeper_manager.domain.planning import TeamWeekState, WeeklyPlan
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.availability_advice import (
    append_availability_trace,
    state_availability_notes,
)
from sleeper_manager.workflows.lock_in_rendering import (
    _evaluation_hash,
    _evaluation_message,
    _evaluation_title,
    _evaluation_trace_json,
    _material_idempotency_key,
)
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    RecommendationRequest,
    recommendation_id_for,
)
from sleeper_manager.workflows.plan_rendering import render_weekly_plan, serialize_weekly_plan_trace
from sleeper_manager.workflows.postgame_types import LOCK_IN_WARNING_TYPE, PostgameLockInResult


async def blocked_postgame_advice(
    state: TeamWeekState,
    plan: WeeklyPlan,
    stable: tuple[LockInOpportunityRecord, ...],
    *,
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    runtime_policy: RuntimePolicy,
    open_sleeper_url: str,
    player_names: dict[str, str],
) -> PostgameLockInResult:
    """Persist warnings for stable games whose full-team constraints cannot be reconciled."""
    results = []
    for row in stable:
        game = next(
            (
                game
                for game in state.opportunities
                if (game.sleeper_player_id, game.game_id) == (row.key.player_id, row.key.game_id)
            ),
            None,
        )
        evaluation = LockInEvaluation(
            state.decision_time,
            LockInEvaluationKind.UNAVAILABLE,
            row.key.player_id,
            row.key.game_id,
            row.action_deadline,
            state.input_version,
            runtime_policy.manager_intent.version,
            ("mapping_unavailable" if game is None else "planning_state_blocked",),
            (("planner_version", plan.planner_version),),
            observed_score=row.stable_score,
        )
        material = _evaluation_hash(evaluation) + plan.material_hash
        identity = _material_idempotency_key(row, evaluation, material)
        if row.current_recommendation_id and row.current_recommendation_id != recommendation_id_for(
            identity
        ):
            await repository.supersede_recommendation(
                row.current_recommendation_id, state.decision_time
            )
        payload = json.loads(_evaluation_trace_json(evaluation, row))
        payload["team_plan"] = json.loads(serialize_weekly_plan_trace(plan, trigger="postgame"))
        notes = state_availability_notes(
            state,
            player_names=player_names,
            local_timezone=ZoneInfo(runtime_policy.manager_timezone),
        )
        trace = append_availability_trace(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), notes, material
        )
        result = await notifications.run(
            RecommendationRequest(
                league_id=state.league_id,
                fantasy_week=state.week,
                player_id=row.key.player_id,
                game_id=row.key.game_id,
                decision_type=LOCK_IN_WARNING_TYPE,
                title=_evaluation_title(evaluation, player_names),
                message=_evaluation_message(evaluation)
                + "\n\n"
                + render_weekly_plan(plan, player_names=player_names).message,
                deadline=row.action_deadline,
                policy_version=runtime_policy.manager_intent.version,
                open_sleeper_url=open_sleeper_url,
                trace_json=trace,
                idempotency_key=identity,
            )
        )
        latest = await repository.get_lock_in_opportunity(row.key)
        if latest is not None:
            await repository.update_lock_in_opportunity(
                replace(
                    latest,
                    status=LockInOpportunityStatus.RECONCILIATION_REQUIRED,
                    current_recommendation_id=result.recommendation.recommendation_id,
                    current_recommendation_kind=LOCK_IN_WARNING_TYPE,
                    latest_evaluation_hash=material,
                    trace_json=trace,
                    updated_at=state.decision_time,
                ),
                expected_version=latest.row_version,
            )
        results.append(
            PostgameLockInResult(
                "delivery_failed" if result.status == "delivery_failed" else "unavailable",
                result.recommendation,
                evaluation,
            )
        )
    return next((result for result in results if result.outcome == "delivery_failed"), results[0])
