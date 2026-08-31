"""D1 implementation of guarded live Lock-In opportunity persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

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
    INSERT_LOCK_IN_OPPORTUNITY_SQL,
    LIST_ACKNOWLEDGED_LOCK_IN_OPPORTUNITIES_SQL,
    LIST_ACTIONABLE_LOCK_IN_OPPORTUNITIES_SQL,
    LIST_DUE_LOCK_IN_OPPORTUNITIES_SQL,
    LOAD_LOCK_IN_OPPORTUNITY_SQL,
    RECORD_LOCK_IN_OBSERVATION_SQL,
    UPDATE_LOCK_IN_OPPORTUNITY_SQL,
)


class D1LockInOpportunityMixin:
    """Add Lock-In opportunity operations to a D1 repository."""

    async def _first(self, query: str, *params: object) -> dict[str, Any] | None:
        """Require the concrete repository's single-row D1 helper."""

        raise NotImplementedError

    async def _all(self, query: str, *params: object) -> Sequence[object]:
        """Require the concrete repository's multi-row D1 helper."""

        raise NotImplementedError

    async def _run(self, query: str, *params: object) -> Any:
        """Require the concrete repository's mutation D1 helper."""

        raise NotImplementedError

    @staticmethod
    def _changes(result: Any) -> int:
        """Require the concrete repository's D1 change-count decoder."""

        raise NotImplementedError

    @staticmethod
    def _mapping(row: object) -> Mapping[str, Any]:
        """Require the concrete repository's D1 row decoder."""

        raise NotImplementedError

    async def upsert_lock_in_opportunity(self, record: LockInOpportunityRecord) -> bool:
        """Insert one opportunity without overwriting a concurrently advanced row."""

        result = await self._run(
            INSERT_LOCK_IN_OPPORTUNITY_SQL,
            *lock_in_opportunity_values(record),
        )
        return self._changes(result) == 1

    async def get_lock_in_opportunity(
        self, key: LockInOpportunityKey
    ) -> LockInOpportunityRecord | None:
        """Load one opportunity by its complete player-game identity."""

        row = await self._first(LOAD_LOCK_IN_OPPORTUNITY_SQL, *lock_in_key_values(key))
        return lock_in_opportunity_from_mapping(row) if row is not None else None

    async def record_lock_in_observation(
        self,
        key: LockInOpportunityKey,
        observation: LockInObservation,
        *,
        expected_version: int,
    ) -> LockInOpportunityRecord | None:
        """Apply one distinct direct poll and stabilize after two matching fingerprints."""

        rows = await self._all(
            RECORD_LOCK_IN_OBSERVATION_SQL,
            *lock_in_observation_values(key, observation, expected_version),
        )
        return lock_in_opportunity_from_mapping(self._mapping(rows[0])) if rows else None

    async def update_lock_in_opportunity(
        self,
        record: LockInOpportunityRecord,
        *,
        expected_version: int,
    ) -> bool:
        """Replace mutable opportunity evidence under compare-and-swap protection."""

        result = await self._run(
            UPDATE_LOCK_IN_OPPORTUNITY_SQL,
            *lock_in_opportunity_update_values(record, expected_version),
        )
        return self._changes(result) == 1

    async def list_due_lock_in_opportunities(
        self, now: datetime, *, limit: int = 100
    ) -> tuple[LockInOpportunityRecord, ...]:
        """List open opportunities ready for the current scheduled wake."""

        if limit <= 0:
            return ()
        rows = await self._all(
            LIST_DUE_LOCK_IN_OPPORTUNITIES_SQL,
            now.isoformat(),
            now.isoformat(),
            limit,
        )
        return tuple(lock_in_opportunity_from_mapping(self._mapping(row)) for row in rows)

    async def list_actionable_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]:
        """List current actionable recommendations in deadline order."""

        rows = await self._all(
            LIST_ACTIONABLE_LOCK_IN_OPPORTUNITIES_SQL,
            league_id,
            fantasy_week,
        )
        return tuple(lock_in_opportunity_from_mapping(self._mapping(row)) for row in rows)

    async def expire_lock_in_opportunities(self, now: datetime) -> int:
        """Expire every unacknowledged opportunity whose safe deadline elapsed."""

        result = await self._run(
            EXPIRE_LOCK_IN_OPPORTUNITIES_SQL,
            now.isoformat(),
            now.isoformat(),
        )
        return self._changes(result)

    async def load_acknowledged_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]:
        """Load fixed Lock and removed Pass evidence for later planning."""

        rows = await self._all(
            LIST_ACKNOWLEDGED_LOCK_IN_OPPORTUNITIES_SQL,
            league_id,
            fantasy_week,
        )
        return tuple(lock_in_opportunity_from_mapping(self._mapping(row)) for row in rows)


__all__ = ["D1LockInOpportunityMixin"]
