"""Observe direct ESPN finals and deliver coordinated team advice from stable scores."""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy
from sleeper_manager.decisions.win_probability import WinObjective
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import calculate_fantasy_points
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInObservation,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.coordinated_postgame import deliver_coordinated_postgame
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs
from sleeper_manager.workflows.postgame_observations import (
    scored_players,
    summary_fingerprint,
    summary_wait_reason,
)
from sleeper_manager.workflows.postgame_types import (
    LOCK_IN_DECISION_TYPE,
    LOCK_IN_WARNING_TYPE,
    DirectGameSummarySource,
    PostgameLockInResult,
)
from sleeper_manager.workflows.recommendation_state import refreshed_team_state
from sleeper_manager.workflows.sleeper_locks import SleeperLockReader, with_sleeper_locks

_RECHECK_DELAY = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class PostgameWait:
    """A claimed game whose current summary cannot count as a final poll."""

    opportunities: tuple[LockInOpportunityRecord, ...]
    reason: str
    active: bool


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
    lock_reader: SleeperLockReader | None = None,
) -> PostgameLockInResult:
    """Observe and evaluate one game for callers without a scheduled batch."""

    await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=decision_time)
    if lock_reader is not None:
        inputs = await with_sleeper_locks(
            inputs, reader=lock_reader, repository=repository, now=decision_time
        )
    stable = await observe_postgame(
        game_id,
        inputs,
        decision_time=decision_time,
        repository=repository,
        fetch_summary=fetch_summary,
        poll_id=poll_id,
    )
    return await recommend_postgame_batch(
        inputs,
        stable,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        runtime_policy=runtime_policy,
        open_sleeper_url=open_sleeper_url,
        player_names=player_names,
        policy=policy,
    )


async def observe_postgame(
    game_id: str,
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
    repository: AsyncRuntimeStateRepository,
    fetch_summary: DirectGameSummarySource,
    poll_id: str,
) -> tuple[LockInOpportunityRecord, ...]:
    """Persist a qualified final before spending time on team optimization."""

    due = await repository.list_due_lock_in_opportunities(decision_time)
    opportunities = tuple(item for item in due if item.key.game_id == game_id)
    if not opportunities:
        return ()
    summary_result = await fetch_summary(game_id, scored_players(opportunities))
    decision_time = max(decision_time, summary_result.quality.retrieved_at)
    if any(row.action_deadline <= decision_time for row in opportunities):
        await repository.expire_lock_in_opportunities(decision_time)
        opportunities = tuple(row for row in opportunities if row.action_deadline > decision_time)
        if not opportunities:
            return ()
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
        return ()

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

    return tuple(updated)


async def read_postgame_wait(
    game_id: str,
    opportunities: tuple[LockInOpportunityRecord, ...],
    *,
    now: datetime,
    fetch_summary: DirectGameSummarySource,
) -> PostgameWait | None:
    """Explain why a game must keep waiting without writing; None when a poll could advance."""

    result = await fetch_summary(game_id, scored_players(opportunities))
    if any(row.action_deadline <= max(now, result.quality.retrieved_at) for row in opportunities):
        return None
    reason = summary_wait_reason(result, opportunities)
    if reason is None:
        return None
    return PostgameWait(opportunities, reason, result.records.game.status is GameStatus.IN_PROGRESS)


async def defer_postgame_wait(
    wait: PostgameWait,
    *,
    repository: AsyncRuntimeStateRepository,
    decision_time: datetime,
    recheck_from: datetime,
) -> None:
    """Persist Wait evidence due again by the next scheduled wake."""

    await _defer_opportunities(
        repository,
        wait.opportunities,
        decision_time=decision_time,
        reason=wait.reason,
        active=wait.active,
        recheck_from=recheck_from,
    )


async def recommend_postgame_batch(
    inputs: LivePlanningInputs,
    stable: tuple[LockInOpportunityRecord, ...],
    *,
    decision_time: datetime,
    repository: AsyncRuntimeStateRepository,
    notifications: NotificationLoop,
    runtime_policy: RuntimePolicy,
    open_sleeper_url: str,
    player_names: dict[str, str] | None = None,
    policy: ScoreMaximizingLockInPolicy | None = None,
    win_objective: WinObjective | None = None,
) -> PostgameLockInResult:
    """Evaluate all observed finals against the same refreshed team state."""
    if not stable:
        return PostgameLockInResult("wait")
    decision_time = max(decision_time, *(row.updated_at for row in stable))
    live_inputs = replace(inputs, runtime_policy_version=runtime_policy.manager_intent.version)
    state = await refreshed_team_state(live_inputs, repository, decision_time)
    return await deliver_coordinated_postgame(
        live_inputs,
        state,
        stable,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        runtime_policy=runtime_policy,
        open_sleeper_url=open_sleeper_url,
        player_names=player_names,
        policy=policy,
        win_objective=win_objective,
    )


async def _defer_opportunities(
    repository: AsyncRuntimeStateRepository,
    opportunities: tuple[LockInOpportunityRecord, ...],
    *,
    decision_time: datetime,
    reason: str,
    active: bool,
    recheck_from: datetime | None = None,
) -> None:
    """Persist Wait evidence without emitting a routine notification."""

    for opportunity in opportunities:
        deferred = replace(
            opportunity,
            status=(
                LockInOpportunityStatus.ACTIVE if active else LockInOpportunityStatus.FINALIZING
            ),
            next_check_at=(recheck_from or decision_time) + _RECHECK_DELAY,
            trace_json=json.dumps({"reason_codes": [reason]}, sort_keys=True),
            updated_at=decision_time,
        )
        await repository.update_lock_in_opportunity(
            deferred,
            expected_version=opportunity.row_version,
        )


__all__ = (
    "DirectGameSummarySource",
    "LOCK_IN_DECISION_TYPE",
    "LOCK_IN_WARNING_TYPE",
    "PostgameLockInResult",
    "PostgameWait",
    "defer_postgame_wait",
    "observe_postgame",
    "read_postgame_wait",
    "recommend_postgame_batch",
    "run_postgame_lock_in",
    "summary_fingerprint",
    "summary_wait_reason",
)
