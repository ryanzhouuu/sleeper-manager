"""Bracket each scheduled tipoff with lineup reads, because no read lands exactly at tipoff."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.lock_in_planning import _merge_planning_evidence

BEFORE_TIPOFF = timedelta(minutes=5)
AFTER_TIPOFF = timedelta(minutes=10)
_AFTER_READ_GRACE = timedelta(minutes=15)
_POSTGAME_DELAY = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class TipoffLineup:
    """One observed lineup; starter_slots maps each starting player to (slot index, position)."""

    league_id: str
    week: int
    roster_id: int
    starter_slots: Mapping[str, tuple[int, str]]
    read_at: datetime


async def record_tipoff_read(
    repository: AsyncRuntimeStateRepository,
    lineup: TipoffLineup,
    *,
    tipoff: datetime,
) -> None:
    """Apply one read to every roster game sharing this scheduled tipoff."""
    rows = await repository.list_week_lock_in_opportunities(lineup.league_id, lineup.week)
    owned = tuple(row for row in rows if row.key.roster_id == lineup.roster_id)
    for row in owned:
        if row.scheduled_start != tipoff:
            continue
        updated = (
            _before_tipoff(row, lineup, owned)
            if lineup.read_at < tipoff
            else _after_tipoff(row, lineup, owned)
        )
        if updated is None:
            continue
        await repository.update_lock_in_opportunity(updated, expected_version=row.row_version)
        if updated.tipoff_unverified:
            await repository.upsert_scheduled_work(_postgame_watch(updated, lineup.read_at))


def _before_tipoff(
    row: LockInOpportunityRecord,
    lineup: TipoffLineup,
    rows: tuple[LockInOpportunityRecord, ...],
) -> LockInOpportunityRecord | None:
    if row.status not in {LockInOpportunityStatus.SCHEDULED, LockInOpportunityStatus.INELIGIBLE}:
        return None
    slot = lineup.starter_slots.get(row.key.player_id)
    candidate = replace(
        row,
        status=LockInOpportunityStatus.SCHEDULED if slot else LockInOpportunityStatus.INELIGIBLE,
        slot_index=slot[0] if slot else None,
        slot_position=slot[1] if slot else None,
        eligible_positions=_known_positions(row, rows) if slot else (),
        rostered_at_tipoff=slot is not None,
        roster_evidence_at=lineup.read_at,
        updated_at=lineup.read_at,
        trace_json=json.dumps(
            {"observed_starter": slot is not None, "observed_at": lineup.read_at.isoformat()}
        ),
    )
    return _merge_planning_evidence(row, candidate)


def _after_tipoff(
    row: LockInOpportunityRecord,
    lineup: TipoffLineup,
    rows: tuple[LockInOpportunityRecord, ...],
) -> LockInOpportunityRecord | None:
    """Benched before and starting after is the only case the reads cannot settle."""
    slot = lineup.starter_slots.get(row.key.player_id)
    trace = json.loads(row.trace_json)
    if (
        slot is None
        or row.status is not LockInOpportunityStatus.INELIGIBLE
        or row.rostered_at_tipoff is not False
        or trace.get("eligibility_forfeited") is True
        or row.roster_evidence_at is None
        or row.roster_evidence_at >= row.scheduled_start
        or lineup.read_at > row.scheduled_start + _AFTER_READ_GRACE
    ):
        return None
    return replace(
        row,
        status=LockInOpportunityStatus.ACTIVE,
        slot_index=slot[0],
        slot_position=slot[1],
        eligible_positions=_known_positions(row, rows),
        rostered_at_tipoff=True,
        tipoff_unverified=True,
        next_check_at=max(row.next_check_at, row.scheduled_start + _POSTGAME_DELAY),
        updated_at=lineup.read_at,
        trace_json=json.dumps({**trace, "eligibility_forfeited": False}),
    )


def _known_positions(
    row: LockInOpportunityRecord, rows: tuple[LockInOpportunityRecord, ...]
) -> tuple[str, ...]:
    """A benched read stores no positions, so reuse the player's other games this week."""
    if row.eligible_positions:
        return row.eligible_positions
    return next(
        (
            other.eligible_positions
            for other in rows
            if other.key.player_id == row.key.player_id and other.eligible_positions
        ),
        (),
    )


def _postgame_watch(row: LockInOpportunityRecord, observed_at: datetime) -> ScheduledWorkRecord:
    """Match the game's shared watch so an unverified game is polled with the others."""
    key = f"postgame:{row.key.game_id}"
    return ScheduledWorkRecord(
        work_id=sha256(key.encode()).hexdigest()[:32],
        dedupe_key=key,
        kind=DueWorkKind.POSTGAME,
        due_at=row.scheduled_start + _POSTGAME_DELAY,
        status=ScheduledWorkStatus.PENDING,
        game_id=row.key.game_id,
        deadline=row.action_deadline,
        created_at=observed_at,
        updated_at=observed_at,
    )
