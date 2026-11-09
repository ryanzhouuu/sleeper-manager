"""Skip full postgame collection while settled watches have nothing new to evaluate.

Once a game's scores are stable and advice is recorded, only Sleeper's submitted locks change
between scheduled runs; pre-tipoff, daily, and other finals replan with fresh evidence anyway.
"""

import sys
from datetime import datetime, timedelta

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.integrations.sleeper.matchup_legs import parse_matchup_legs
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, ScheduledWorkRecord
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.sleeper_locks import SleeperLockReader, is_sleeper_synced_lock

FULL_PASS_INTERVAL = timedelta(minutes=30)


async def settled_postgame_unchanged(
    work_items: tuple[ScheduledWorkRecord, ...],
    *,
    repository: AsyncRuntimeStateRepository,
    lock_reader: SleeperLockReader | None,
    now: datetime,
) -> bool:
    """True only when every claimed game is settled and Sleeper's locks match stored rows."""
    games = {work.game_id for work in work_items}
    if lock_reader is None or not games or None in games:
        return False
    due = await repository.list_due_lock_in_opportunities(now)
    rows = tuple(row for row in due if row.key.game_id in games)
    if {row.key.game_id for row in rows} != games or not all(_settled(row, now) for row in rows):
        return False
    scopes = {(row.key.league_id, row.key.fantasy_week, row.key.roster_id) for row in rows}
    if len(scopes) != 1:
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


def _settled(row: LockInOpportunityRecord, now: datetime) -> bool:
    """A periodic full pass still refreshes injuries and lineup evidence for settled games."""
    return (
        row.status is LockInOpportunityStatus.ACTIONABLE
        and row.stable_score is not None
        and row.current_recommendation_id is not None
        and row.current_observed_at is not None
        and now - row.current_observed_at < FULL_PASS_INTERVAL
    )
