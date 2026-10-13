"""Reconcile roster tipoff watches and preserve stable scheduled-work identities."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256

from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs


async def _replace_future_pre_tipoff(
    repository: AsyncRuntimeStateRepository,
    policy: RuntimePolicy,
    inputs: LivePlanningInputs,
    scheduled_at: datetime,
) -> None:
    """Replace future watches from the latest roster schedule without reviving closed work."""
    wanted = {
        game.provider_id: game
        for game in _roster_games(inputs)
        if game.start_time > scheduled_at and game.status is GameStatus.SCHEDULED
    }
    existing = await repository.list_scheduled_work(
        kind=DueWorkKind.PRE_TIPOFF,
        statuses=(ScheduledWorkStatus.PENDING, ScheduledWorkStatus.RETRY),
    )
    for work in existing:
        if work.game_id in wanted:
            continue
        if work.due_at <= scheduled_at and (work.deadline is None or work.deadline <= scheduled_at):
            continue
        await repository.upsert_scheduled_work(
            replace(work, status=ScheduledWorkStatus.CANCELED, updated_at=scheduled_at)
        )
    for game in wanted.values():
        key = f"pre_tipoff:{game.provider_id}"
        await repository.upsert_scheduled_work(
            ScheduledWorkRecord(
                work_id=_work_id(key),
                dedupe_key=key,
                kind=DueWorkKind.PRE_TIPOFF,
                due_at=game.start_time - policy.move_lead_time,
                status=ScheduledWorkStatus.PENDING,
                game_id=game.provider_id,
                deadline=game.start_time,
                created_at=scheduled_at,
                updated_at=scheduled_at,
            )
        )


def _roster_games(inputs: LivePlanningInputs) -> tuple[ScheduledGame, ...]:
    """Scope watches to resolved teams represented on the manager roster."""
    roster = next(
        item
        for item in inputs.league_profile.rosters
        if item.roster_id == inputs.league_profile.manager_roster_id
    )
    rostered = set(roster.player_ids)
    team_ids = {
        identity.provider_team_id
        for identity in inputs.identities
        if identity.sleeper_player_id in rostered and identity.provider_team_id
    }
    games: dict[str, ScheduledGame] = {}
    for result in inputs.schedule_results:
        for game in result.games:
            if game.home_team_id in team_ids or game.away_team_id in team_ids:
                games[game.provider_id] = game
    return tuple(games.values())


def _work_id(dedupe_key: str) -> str:
    return sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
