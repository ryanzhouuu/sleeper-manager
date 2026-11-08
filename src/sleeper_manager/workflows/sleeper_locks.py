"""Make Sleeper's submitted locks the source of truth for live opportunity rows.

Sleeper names locked games by its own game ids, while opportunity rows use ESPN event ids.
A player plays at most once per US Eastern date, so a lock matches the row for the same player
whose game falls on the locked game's Eastern date (taken from Sleeper's stat row).
"""

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.sleeper.game_stats import (
    box_score_line,
    game_date_by_id,
    game_stats_by_id,
)
from sleeper_manager.integrations.sleeper.matchup_legs import (
    MatchupLeg,
    parse_matchup_legs,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.lock_in_evidence import merge_lock_in_opportunity_evidence
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs

LOCK_SOURCE_SLEEPER = "sleeper"
_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class LockReconciliation:
    """Rows to write, plus anomalies: ``(player_id, sleeper_game_id)`` locks with no row and
    ``(player_id, game_id)`` Sleeper-synced rows whose lock no longer appears."""

    updates: tuple[LockInOpportunityRecord, ...]
    missing_rows: tuple[tuple[str, str], ...]
    vanished_locks: tuple[tuple[str, str], ...]


class SleeperLockReader(Protocol):
    async def matchup_legs(self, league_id: str, week: int) -> object: ...

    async def player_game_stats(self, player_id: str, season: int) -> dict[str, Any]: ...


async def with_sleeper_locks(
    inputs: LivePlanningInputs,
    *,
    reader: SleeperLockReader,
    repository: AsyncRuntimeStateRepository,
    now: datetime,
) -> LivePlanningInputs:
    """Sync locks, then plan from them; without lock data the plan must degrade."""
    if not await sync_sleeper_locks(inputs, reader=reader, repository=repository, now=now):
        return replace(inputs, sleeper_locks_unavailable=True)
    records = await repository.load_acknowledged_lock_in_opportunities(
        inputs.league_profile.league_id, inputs.week_window.week
    )
    return replace(
        inputs,
        acknowledgements=merge_lock_in_opportunity_evidence(inputs.acknowledgements, records),
    )


async def sync_sleeper_locks(
    inputs: LivePlanningInputs,
    *,
    reader: SleeperLockReader,
    repository: AsyncRuntimeStateRepository,
    now: datetime,
) -> bool:
    """Apply Sleeper's locks to stored rows; False when Sleeper's lock data is unavailable."""
    profile = inputs.league_profile
    week = inputs.week_window.week
    try:
        legs = parse_matchup_legs(await reader.matchup_legs(profile.league_id, week))
    except Exception as error:
        print(f"Sleeper lock sync unavailable: {type(error).__name__}", file=sys.stderr)
        return False
    leg = next((item for item in legs if item.roster_id == profile.manager_roster_id), None)
    if leg is None:
        print("Sleeper lock sync unavailable: manager roster missing", file=sys.stderr)
        return False
    season = nba_season_start_year(now.astimezone(_EASTERN).date())
    lock_dates: dict[str, date] = {}
    lock_scores: dict[str, float] = {}
    for player_id, game_id in leg.starters_games.items():
        game_date, score = await _locked_game(reader, player_id, game_id, season, profile.scoring)
        if game_date is not None:
            lock_dates[player_id] = game_date
        if score is not None:
            lock_scores[player_id] = score
    positions = [
        slot.position
        for slot in sorted(profile.roster_slots, key=lambda slot: slot.index)
        if slot.is_starting
    ]
    records = await repository.list_week_lock_in_opportunities(profile.league_id, week)
    result = reconcile_sleeper_locks(
        records, leg, positions, lock_dates=lock_dates, lock_scores=lock_scores, now=now
    )
    for record in result.updates:
        await repository.update_lock_in_opportunity(record, expected_version=record.row_version)
    if result.missing_rows or result.vanished_locks:
        print(
            "Sleeper lock anomalies: "
            f"missing_rows={[pid for pid, _ in result.missing_rows]} "
            f"vanished_locks={list(result.vanished_locks)}",
            file=sys.stderr,
        )
    return True


async def _locked_game(
    reader: SleeperLockReader, player_id: str, game_id: str, season: int, scoring: ScoringPolicy
) -> tuple[date | None, float | None]:
    """The locked game's Eastern date and Sleeper score; a late or failed read retries later."""
    try:
        payload = await reader.player_game_stats(player_id, season)
        stats = game_stats_by_id(payload, game_id)
        score = calculate_fantasy_points(box_score_line(stats), scoring) if stats else None
        return game_date_by_id(payload, game_id), score
    except Exception as error:
        print(f"Sleeper lock game unavailable: {type(error).__name__}", file=sys.stderr)
        return None, None


def eastern_game_date(record: LockInOpportunityRecord) -> date:
    return record.scheduled_start.astimezone(_EASTERN).date()


def reconcile_sleeper_locks(
    records: tuple[LockInOpportunityRecord, ...],
    leg: MatchupLeg,
    roster_positions: Sequence[str],
    *,
    lock_dates: Mapping[str, date],
    now: datetime,
    lock_scores: Mapping[str, float] | None = None,
) -> LockReconciliation:
    """``lock_dates`` maps each locked player to the Eastern date of the locked game.

    ``lock_scores`` are Sleeper's scores for those games; they fill rows whose own score has
    not stabilized, because lock evidence without a score would block planning.
    """
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
            score = (lock_scores or {}).get(player_id)
            updates.append(_locked(rows[index], leg, roster_positions, sleeper_game_id, score, now))
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
    sleeper_score: float | None,
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
    if record.stable_score is None and sleeper_score is not None:
        record = replace(record, stable_score=round(sleeper_score, 6), stabilized_at=now)
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
