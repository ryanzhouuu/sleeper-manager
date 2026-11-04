"""Deliver one complete postgame recommendation with per-player confirmation identities."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy
from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.lock_in import LockInDecision, LockInEvaluation, LockInEvaluationKind
from sleeper_manager.domain.planning import PlanStatus, TeamWeekState
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.daily_plan import _run_weekly_lineup_workflow
from sleeper_manager.workflows.decision_inputs import decision_input_fingerprint
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs
from sleeper_manager.workflows.planning_policy import LIVE_PLAN_POLICY
from sleeper_manager.workflows.postgame_types import PostgameLockInResult
from sleeper_manager.workflows.postgame_warnings import blocked_postgame_advice
from sleeper_manager.workflows.score_confirmations import restore_score_confirmations
from sleeper_manager.workflows.sticky_advice import published_team_advice


async def deliver_coordinated_postgame(
    live_inputs: LivePlanningInputs,
    state: TeamWeekState,
    stable: tuple[LockInOpportunityRecord, ...],
    *,
    decision_time: datetime,
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    runtime_policy: RuntimePolicy,
    open_sleeper_url: str,
    player_names: dict[str, str] | None,
    policy: ScoreMaximizingLockInPolicy | None,
) -> PostgameLockInResult:
    """Refresh the whole team instead of composing independent player optimizations."""
    config = WeeklyPlanPolicyConfig(
        scenario_count=policy.config.scenario_count if policy else LIVE_PLAN_POLICY.scenario_count,
        postgame_scenario_count=(
            policy.config.scenario_count if policy else LIVE_PLAN_POLICY.postgame_scenario_count
        ),
        seed=policy.config.seed if policy else 0,
        minimum_lock_in_confidence=runtime_policy.manager_intent.minimum_confidence,
    )
    if not state.is_blocked and not state.recovery_obligations:
        published = await published_team_advice(
            repository, state.league_id, state.week, now=decision_time
        )
        if published is not None and published[1].get(
            "input_fingerprint"
        ) == decision_input_fingerprint(state, config):
            children = await restore_score_confirmations(
                repository, published[1], now=decision_time
            )
            observed = {(row.key.player_id, row.key.game_id) for row in stable}
            return PostgameLockInResult(
                "duplicate",
                next((row for row in children if (row.player_id, row.game_id) in observed), None),
            )
    coordinated = build_team_recommendation(
        state, lead_time=live_inputs.move_lead_time, policy=config
    )
    if coordinated.status is PlanStatus.BLOCKED:
        return await blocked_postgame_advice(
            state,
            coordinated,
            stable,
            repository=repository,
            notifications=notifications,
            runtime_policy=runtime_policy,
            open_sleeper_url=open_sleeper_url,
            player_names=player_names or {},
        )
    result = await _run_weekly_lineup_workflow(
        live_inputs,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url=open_sleeper_url,
        player_names=player_names,
        local_timezone=ZoneInfo(runtime_policy.manager_timezone),
        policy=config,
        clock=lambda: decision_time,
        trigger="postgame",
        prepared_plan=coordinated,
        prepared_state=state,
    )
    action = next(
        (
            action
            for action in result.plan.score_actions
            if any(
                (row.key.player_id, row.key.game_id) == (action.player_id, action.game_id)
                for row in stable
            )
        ),
        None,
    )
    if action is None:
        return PostgameLockInResult("wait")
    recommendation = None
    if result.recommendation is not None:
        identities = json.loads(result.recommendation.trace_json).get("confirmation_targets", [])
        for identity in identities:
            child = await repository.get_recommendation(identity)
            if (
                child is not None
                and child.player_id == action.player_id
                and child.game_id == action.game_id
            ):
                recommendation = child
                break
    evaluation = LockInEvaluation(
        decision_time=decision_time,
        kind=LockInEvaluationKind(action.kind.value),
        player_id=action.player_id,
        game_id=action.game_id,
        deadline=action.deadline,
        information_version=state.input_version,
        manager_policy_version=runtime_policy.manager_intent.version,
        reason_codes=("joint_team_comparison",),
        trace=(("planner_version", result.plan.planner_version),),
        observed_score=action.score,
        alternative_expected_score=action.alternative_expected_score,
        confidence=action.confidence or 0,
        decision=LockInDecision(
            decision_time,
            action.kind,
            action.player_id,
            action.game_id,
            action.slot_index,
            result.plan.expected_terminal_score or 0,
            (result.plan.expected_terminal_score or 0) - (action.alternative_expected_score or 0),
            state.input_version,
            "Joint executable team recommendation.",
        ),
    )
    return PostgameLockInResult(
        "delivery_failed"
        if result.outcome == "delivery_failed"
        else "duplicate"
        if result.outcome == "duplicate"
        else "notified",
        recommendation,
        evaluation,
    )
