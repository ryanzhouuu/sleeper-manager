"""Derive each player-game's dashboard state from stored rows; nothing here is manual input."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from sleeper_manager.workflows.sleeper_locks import eastern_game_date

GameStateName = Literal[
    "locked",
    "lockable",
    "passed_eligibility_lost",
    "passed_window_closed",
    "not_eligible",
    "unverified",
    "upcoming",
    "live",
]
_SCORED = frozenset({Status.ACTIONABLE, Status.FINALIZING, Status.AUTOMATIC_FINAL})


@dataclass(frozen=True, slots=True)
class GameState:
    """``overridable`` games accept the owner's tipoff-eligibility answer."""

    state: GameStateName
    deadline: datetime | None
    overridable: bool


def game_state(
    record: LockInOpportunityRecord,
    now: datetime,
    *,
    records: Sequence[LockInOpportunityRecord],
    sleeper_lock_date: date | None = None,
) -> GameState:
    """``sleeper_lock_date`` is a live Sleeper lock read newer than the stored row."""
    if record.status is Status.ACKNOWLEDGED_LOCKED or (
        sleeper_lock_date is not None and eastern_game_date(record) == sleeper_lock_date
    ):
        return GameState("locked", None, False)
    if record.scheduled_start > now:
        return GameState("upcoming", None, False)
    if sleeper_lock_date is not None or _player_locked(record, records):
        # A player locks at most one game per week, so his other games no longer count.
        return GameState("passed_window_closed", None, False)
    closes = min(record.action_deadline, record.fantasy_week_end)
    window_open = now < closes and not _next_game_started(record, records, now)
    if record.tipoff_unverified:
        return GameState("unverified", closes if window_open else None, window_open)
    if record.status is Status.INELIGIBLE:
        lost = record.rostered_at_tipoff is True
        return GameState("passed_eligibility_lost" if lost else "not_eligible", None, False)
    if record.rostered_at_tipoff is False:
        return GameState("not_eligible", None, False)
    if record.rostered_at_tipoff is None:
        return GameState("unverified", None, False)
    if record.status is Status.EXPIRED or not window_open:
        return GameState("passed_window_closed", None, False)
    if record.status in _SCORED and record.stable_score is not None:
        return GameState("lockable", closes, False)
    return GameState("live", None, False)


def _next_game_started(
    record: LockInOpportunityRecord, records: Sequence[LockInOpportunityRecord], now: datetime
) -> bool:
    return any(
        other.key.player_id == record.key.player_id
        and record.scheduled_start < other.scheduled_start <= now
        for other in records
    )


def _player_locked(
    record: LockInOpportunityRecord, records: Sequence[LockInOpportunityRecord]
) -> bool:
    return any(
        other.key.player_id == record.key.player_id
        and other.key.game_id != record.key.game_id
        and other.status is Status.ACKNOWLEDGED_LOCKED
        for other in records
    )
