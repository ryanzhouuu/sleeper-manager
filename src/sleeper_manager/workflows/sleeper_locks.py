"""Make Sleeper's submitted locks the source of truth for live opportunity rows.

Sleeper names locked games by its own game ids, while opportunity rows use ESPN event ids.
A player plays at most once per US Eastern date, so a lock matches the row for the same player
whose game falls on the locked game's Eastern date (taken from Sleeper's stat row).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.integrations.sleeper.matchup_legs import MatchupLeg
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision

LOCK_SOURCE_SLEEPER = "sleeper"
_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class LockReconciliation:
    """Rows to write, plus anomalies: ``(player_id, sleeper_game_id)`` locks with no row and
    ``(player_id, game_id)`` Sleeper-synced rows whose lock no longer appears."""

    updates: tuple[LockInOpportunityRecord, ...]
    missing_rows: tuple[tuple[str, str], ...]
    vanished_locks: tuple[tuple[str, str], ...]


def eastern_game_date(record: LockInOpportunityRecord) -> date:
    return record.scheduled_start.astimezone(_EASTERN).date()


def reconcile_sleeper_locks(
    records: tuple[LockInOpportunityRecord, ...],
    leg: MatchupLeg,
    roster_positions: Sequence[str],
    *,
    lock_dates: Mapping[str, date],
    now: datetime,
) -> LockReconciliation:
    """``lock_dates`` maps each locked player to the Eastern date of the locked game."""
    rows = [row for row in records if row.key.roster_id == leg.roster_id]
    matched: set[int] = set()
    updates: list[LockInOpportunityRecord] = []
    missing: list[tuple[str, str]] = []
    for player_id, sleeper_game_id in leg.starters_games.items():
        game_date = lock_dates.get(player_id)
        index = next(
            (
                position
                for position, row in enumerate(rows)
                if row.key.player_id == player_id
                and game_date is not None
                and eastern_game_date(row) == game_date
            ),
            None,
        )
        if index is None:
            missing.append((player_id, sleeper_game_id))
            continue
        matched.add(index)
        if not _synced(rows[index]):
            updates.append(_locked(rows[index], leg, roster_positions, sleeper_game_id, now))
    vanished: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if index in matched:
            continue
        if _synced(row):
            vanished.append((row.key.player_id, row.key.game_id))
        elif _stale_manual_decision(row, now):
            updates.append(reported_decision(row, ManagerAction.NOT_LOCKED, now, actual_slot=None))
    return LockReconciliation(tuple(updates), tuple(missing), tuple(vanished))


def lock_source(record: LockInOpportunityRecord) -> str | None:
    try:
        trace = json.loads(record.trace_json)
    except json.JSONDecodeError:
        return None
    source = trace.get("lock_source") if isinstance(trace, dict) else None
    return source if isinstance(source, str) else None


def _synced(record: LockInOpportunityRecord) -> bool:
    return (
        record.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        and lock_source(record) == LOCK_SOURCE_SLEEPER
    )


def _stale_manual_decision(record: LockInOpportunityRecord, now: datetime) -> bool:
    """Manual locks Sleeper does not confirm, and manual passes whose window is still open."""
    if now >= record.fantasy_week_end:
        return False
    if record.acknowledged_action == ManagerAction.LOCKED.value:
        return True
    return record.acknowledged_action == ManagerAction.PASSED.value and now < record.action_deadline


def _locked(
    record: LockInOpportunityRecord,
    leg: MatchupLeg,
    roster_positions: Sequence[str],
    sleeper_game_id: str,
    now: datetime,
) -> LockInOpportunityRecord:
    slot_index, slot_position = record.slot_index, record.slot_position
    if record.key.player_id in leg.starters:
        slot_index = leg.starters.index(record.key.player_id)
        if slot_index < len(roster_positions):
            slot_position = roster_positions[slot_index]
    trace = json.loads(record.trace_json)
    trace.update(lock_source=LOCK_SOURCE_SLEEPER, sleeper_game_id=sleeper_game_id)
    if "eligibility_forfeited" in trace:
        trace["eligibility_forfeited"] = False
    return replace(
        record,
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        acknowledged_action=ManagerAction.LOCKED.value,
        acknowledged_at=now,
        slot_index=slot_index,
        slot_position=slot_position,
        rostered_at_tipoff=True,
        tipoff_unverified=False,
        next_check_at=now,
        latest_evaluation_hash=None,
        updated_at=now,
        trace_json=json.dumps(trace),
    )
