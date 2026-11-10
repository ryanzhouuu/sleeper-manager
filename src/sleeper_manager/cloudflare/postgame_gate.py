"""Skip full postgame collection while claimed watches have nothing new to evaluate.

Once a game's scores are stable and advice is recorded, only Sleeper's submitted locks change
between scheduled runs; pre-tipoff, daily, and other finals replan with fresh evidence anyway.
A game whose summary is not yet a usable final only records its wait, which needs no team state.
"""

import sys
from datetime import datetime

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.integrations.sleeper.matchup_legs import parse_matchup_legs
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, ScheduledWorkRecord
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.postgame_lock_in import (
    PostgameWait,
    defer_postgame_wait,
    read_postgame_wait,
)
from sleeper_manager.workflows.postgame_types import DirectGameSummarySource
from sleeper_manager.workflows.sleeper_locks import SleeperLockReader, is_sleeper_synced_lock


async def unchanged_postgame_detail(
    work_items: tuple[ScheduledWorkRecord, ...],
    *,
    repository: AsyncRuntimeStateRepository,
    lock_reader: SleeperLockReader | None,
    fetch_summary: DirectGameSummarySource,
    now: datetime,
    recheck_from: datetime,
) -> str | None:
    """Defer unfinished games and name the skip; None when any game needs the full pass."""
    rows = await _due_rows_by_game(work_items, repository, now)
    if rows is None:
        return None
    settled = tuple(
        row for game_rows in rows.values() if _all_settled(game_rows) for row in game_rows
    )
    waits: list[PostgameWait] = []
    for game_id, game_rows in rows.items():
        if _all_settled(game_rows):
            continue
        try:
            wait = await read_postgame_wait(
                game_id, game_rows, now=now, fetch_summary=fetch_summary
            )
        except Exception as error:
            print(f"Postgame wait check unavailable: {type(error).__name__}", file=sys.stderr)
            return None
        if wait is None:
            return None
        waits.append(wait)
    if settled and not await _sleeper_locks_match(settled, repository, lock_reader):
        return None
    for wait in waits:
        await defer_postgame_wait(
            wait, repository=repository, decision_time=now, recheck_from=recheck_from
        )
    return "awaiting_final" if waits else "settled_unchanged"


async def _due_rows_by_game(
    work_items: tuple[ScheduledWorkRecord, ...],
    repository: AsyncRuntimeStateRepository,
    now: datetime,
) -> dict[str, tuple[LockInOpportunityRecord, ...]] | None:
    games = {work.game_id for work in work_items}
    if not games or None in games:
        return None
    due = await repository.list_due_lock_in_opportunities(now)
    rows = {
        game_id: tuple(row for row in due if row.key.game_id == game_id)
        for game_id in games
        if game_id is not None
    }
    return rows if all(rows.values()) else None


async def _sleeper_locks_match(
    rows: tuple[LockInOpportunityRecord, ...],
    repository: AsyncRuntimeStateRepository,
    lock_reader: SleeperLockReader | None,
) -> bool:
    scopes = {(row.key.league_id, row.key.fantasy_week, row.key.roster_id) for row in rows}
    if lock_reader is None or len(scopes) != 1:
        return False
    ((league_id, week, roster_id),) = scopes
    try:
        legs = parse_matchup_legs(await lock_reader.matchup_legs(league_id, week))
    except Exception as error:
        print(f"Settled postgame check unavailable: {type(error).__name__}", file=sys.stderr)
        return False
    leg = next((item for item in legs if item.roster_id == roster_id), None)
    if leg is None:
        return False
    stored = await repository.list_week_lock_in_opportunities(league_id, week)
    synced = {
        row.key.player_id
        for row in stored
        if row.key.roster_id == roster_id and is_sleeper_synced_lock(row)
    }
    return set(leg.starters_games) == synced


def _all_settled(rows: tuple[LockInOpportunityRecord, ...]) -> bool:
    return all(map(_settled, rows))


def _settled(row: LockInOpportunityRecord) -> bool:
    return (
        row.status is LockInOpportunityStatus.ACTIONABLE
        and row.stable_score is not None
        and row.current_recommendation_id is not None
    )
