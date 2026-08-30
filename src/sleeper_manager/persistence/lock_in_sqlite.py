"""SQLite implementation of guarded live Lock-In opportunity persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from sleeper_manager.persistence.lock_in_opportunities import (
    LockInObservation,
    LockInOpportunityKey,
    LockInOpportunityRecord,
    lock_in_key_values,
    lock_in_observation_values,
    lock_in_opportunity_from_mapping,
    lock_in_opportunity_update_values,
    lock_in_opportunity_values,
)
from sleeper_manager.persistence.lock_in_statements import (
    EXPIRE_LOCK_IN_OPPORTUNITIES_SQL,
    HAS_OPEN_LOCK_IN_WATCH_SQL,
    INSERT_LOCK_IN_OPPORTUNITY_SQL,
    LIST_ACKNOWLEDGED_LOCK_IN_OPPORTUNITIES_SQL,
    LIST_ACTIONABLE_LOCK_IN_OPPORTUNITIES_SQL,
    LIST_DUE_LOCK_IN_OPPORTUNITIES_SQL,
    LOAD_LOCK_IN_OPPORTUNITY_SQL,
    RECORD_LOCK_IN_OBSERVATION_SQL,
    UPDATE_LOCK_IN_OPPORTUNITY_SQL,
)


class _SQLiteConnectionOwner(Protocol):
    """Describe the connection hook supplied by the concrete repository."""

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]: ...


class SQLiteLockInOpportunityMixin:
    """Add Lock-In opportunity operations to a SQLite repository."""

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        """Require the concrete repository to provide a configured connection."""

        raise NotImplementedError

    @staticmethod
    def _records(
        rows: Iterator[sqlite3.Row] | list[sqlite3.Row],
    ) -> tuple[LockInOpportunityRecord, ...]:
        """Decode SQLite rows into immutable opportunity records."""

        return tuple(lock_in_opportunity_from_mapping(dict(row)) for row in rows)

    def upsert_lock_in_opportunity(self, record: LockInOpportunityRecord) -> bool:
        """Insert one opportunity without overwriting a concurrently advanced row."""

        with self._connect() as connection:
            cursor = connection.execute(
                INSERT_LOCK_IN_OPPORTUNITY_SQL,
                lock_in_opportunity_values(record),
            )
        return cursor.rowcount == 1

    def get_lock_in_opportunity(self, key: LockInOpportunityKey) -> LockInOpportunityRecord | None:
        """Load one opportunity by its complete player-game identity."""

        with self._connect() as connection:
            row = connection.execute(
                LOAD_LOCK_IN_OPPORTUNITY_SQL,
                lock_in_key_values(key),
            ).fetchone()
        return lock_in_opportunity_from_mapping(dict(row)) if row is not None else None

    def record_lock_in_observation(
        self,
        key: LockInOpportunityKey,
        observation: LockInObservation,
        *,
        expected_version: int,
    ) -> LockInOpportunityRecord | None:
        """Apply one distinct direct poll and stabilize after two matching fingerprints."""

        with self._connect() as connection:
            row = connection.execute(
                RECORD_LOCK_IN_OBSERVATION_SQL,
                lock_in_observation_values(key, observation, expected_version),
            ).fetchone()
        return lock_in_opportunity_from_mapping(dict(row)) if row is not None else None

    def update_lock_in_opportunity(
        self,
        record: LockInOpportunityRecord,
        *,
        expected_version: int,
    ) -> bool:
        """Replace mutable opportunity evidence under compare-and-swap protection."""

        with self._connect() as connection:
            cursor = connection.execute(
                UPDATE_LOCK_IN_OPPORTUNITY_SQL,
                lock_in_opportunity_update_values(record, expected_version),
            )
        return cursor.rowcount == 1

    def list_due_lock_in_opportunities(
        self, now: datetime, *, limit: int = 100
    ) -> tuple[LockInOpportunityRecord, ...]:
        """List open opportunities ready for the current scheduled wake."""

        if limit <= 0:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                LIST_DUE_LOCK_IN_OPPORTUNITIES_SQL,
                (now.isoformat(), now.isoformat(), limit),
            ).fetchall()
        return self._records(iter(rows))

    def list_actionable_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]:
        """List current actionable recommendations in deadline order."""

        with self._connect() as connection:
            rows = connection.execute(
                LIST_ACTIONABLE_LOCK_IN_OPPORTUNITIES_SQL,
                (league_id, fantasy_week),
            ).fetchall()
        return self._records(iter(rows))

    def expire_lock_in_opportunities(self, now: datetime) -> int:
        """Expire every unacknowledged opportunity whose safe deadline elapsed."""

        with self._connect() as connection:
            cursor = connection.execute(
                EXPIRE_LOCK_IN_OPPORTUNITIES_SQL,
                (now.isoformat(), now.isoformat()),
            )
        return cursor.rowcount

    def has_open_lock_in_watch(self, game_id: str, now: datetime) -> bool:
        """Report whether any player for this game still needs a five-minute watch."""

        with self._connect() as connection:
            row = connection.execute(
                HAS_OPEN_LOCK_IN_WATCH_SQL,
                (game_id, now.isoformat()),
            ).fetchone()
        return row is not None

    def load_acknowledged_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]:
        """Load locked, passed, and automatic-final evidence for later planning."""

        with self._connect() as connection:
            rows = connection.execute(
                LIST_ACKNOWLEDGED_LOCK_IN_OPPORTUNITIES_SQL,
                (league_id, fantasy_week),
            ).fetchall()
        return self._records(iter(rows))


__all__ = ["SQLiteLockInOpportunityMixin"]
