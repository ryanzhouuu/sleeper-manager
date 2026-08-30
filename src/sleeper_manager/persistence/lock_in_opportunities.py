"""Durable live Lock-In opportunity records shared by SQLite and D1."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sleeper_manager.domain.lock_in import LockInOpportunityStatus


@dataclass(frozen=True, slots=True)
class LockInOpportunityKey:
    """Identify one rostered player-game opportunity within a fantasy week."""

    league_id: str
    fantasy_week: int
    roster_id: int
    player_id: str
    game_id: str


@dataclass(frozen=True, slots=True)
class LockInObservation:
    """Describe one direct provider poll used for score stabilization."""

    score: float
    fingerprint: str
    poll_id: str
    observed_at: datetime
    next_check_at: datetime


@dataclass(frozen=True, slots=True)
class LockInOpportunityRecord:
    """Store the current lifecycle and evidence for one live opportunity."""

    key: LockInOpportunityKey
    provider_player_id: str
    scheduled_start: datetime
    action_deadline: datetime
    fantasy_week_end: datetime
    status: LockInOpportunityStatus
    next_check_at: datetime
    created_at: datetime
    updated_at: datetime
    slot_index: int | None = None
    slot_position: str | None = None
    eligible_positions: tuple[str, ...] = ()
    rostered_at_tipoff: bool | None = None
    roster_evidence_at: datetime | None = None
    league_configuration_fingerprint: str | None = None
    current_observed_score: float | None = None
    current_observation_fingerprint: str | None = None
    consecutive_direct_poll_count: int = 0
    current_poll_id: str | None = None
    previous_observed_at: datetime | None = None
    current_observed_at: datetime | None = None
    stable_score: float | None = None
    stable_fingerprint: str | None = None
    stabilized_at: datetime | None = None
    score_revision: int = 0
    current_recommendation_id: str | None = None
    current_recommendation_kind: str | None = None
    acknowledged_action: str | None = None
    acknowledged_at: datetime | None = None
    latest_evaluation_hash: str | None = None
    trace_json: str = "{}"
    row_version: int = 0


class LockInOpportunityRepository(Protocol):
    """Synchronous guarded persistence surface for live Lock-In state."""

    def upsert_lock_in_opportunity(self, record: LockInOpportunityRecord) -> bool: ...

    def get_lock_in_opportunity(
        self, key: LockInOpportunityKey
    ) -> LockInOpportunityRecord | None: ...

    def record_lock_in_observation(
        self,
        key: LockInOpportunityKey,
        observation: LockInObservation,
        *,
        expected_version: int,
    ) -> LockInOpportunityRecord | None: ...

    def update_lock_in_opportunity(
        self,
        record: LockInOpportunityRecord,
        *,
        expected_version: int,
    ) -> bool: ...

    def list_due_lock_in_opportunities(
        self, now: datetime, *, limit: int = 100
    ) -> tuple[LockInOpportunityRecord, ...]: ...

    def list_actionable_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]: ...

    def expire_lock_in_opportunities(self, now: datetime) -> int: ...

    def has_open_lock_in_watch(self, game_id: str, now: datetime) -> bool: ...

    def load_acknowledged_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]: ...


class AsyncLockInOpportunityRepository(Protocol):
    """Async guarded persistence surface used by the Worker runtime."""

    async def upsert_lock_in_opportunity(self, record: LockInOpportunityRecord) -> bool: ...

    async def get_lock_in_opportunity(
        self, key: LockInOpportunityKey
    ) -> LockInOpportunityRecord | None: ...

    async def record_lock_in_observation(
        self,
        key: LockInOpportunityKey,
        observation: LockInObservation,
        *,
        expected_version: int,
    ) -> LockInOpportunityRecord | None: ...

    async def update_lock_in_opportunity(
        self,
        record: LockInOpportunityRecord,
        *,
        expected_version: int,
    ) -> bool: ...

    async def list_due_lock_in_opportunities(
        self, now: datetime, *, limit: int = 100
    ) -> tuple[LockInOpportunityRecord, ...]: ...

    async def list_actionable_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]: ...

    async def expire_lock_in_opportunities(self, now: datetime) -> int: ...

    async def has_open_lock_in_watch(self, game_id: str, now: datetime) -> bool: ...

    async def load_acknowledged_lock_in_opportunities(
        self, league_id: str, fantasy_week: int
    ) -> tuple[LockInOpportunityRecord, ...]: ...


def lock_in_opportunity_values(record: LockInOpportunityRecord) -> tuple[object, ...]:
    """Encode one opportunity in table column order."""

    key = record.key
    return (
        key.league_id,
        key.fantasy_week,
        key.roster_id,
        key.player_id,
        key.game_id,
        record.provider_player_id,
        record.scheduled_start.isoformat(),
        record.action_deadline.isoformat(),
        record.fantasy_week_end.isoformat(),
        record.status.value,
        record.next_check_at.isoformat(),
        record.slot_index,
        record.slot_position,
        json.dumps(record.eligible_positions, separators=(",", ":")),
        None if record.rostered_at_tipoff is None else int(record.rostered_at_tipoff),
        _datetime_value(record.roster_evidence_at),
        record.league_configuration_fingerprint,
        record.current_observed_score,
        record.current_observation_fingerprint,
        record.consecutive_direct_poll_count,
        record.current_poll_id,
        _datetime_value(record.previous_observed_at),
        _datetime_value(record.current_observed_at),
        record.stable_score,
        record.stable_fingerprint,
        _datetime_value(record.stabilized_at),
        record.score_revision,
        record.current_recommendation_id,
        record.current_recommendation_kind,
        record.acknowledged_action,
        _datetime_value(record.acknowledged_at),
        record.latest_evaluation_hash,
        record.trace_json,
        record.row_version,
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
    )


def lock_in_opportunity_update_values(
    record: LockInOpportunityRecord, expected_version: int
) -> tuple[object, ...]:
    """Encode a full guarded update without replacing immutable identity or creation time."""

    values = lock_in_opportunity_values(record)
    return values[5:33] + (values[35],) + values[:5] + (expected_version,)


def lock_in_opportunity_from_mapping(row: Mapping[str, Any]) -> LockInOpportunityRecord:
    """Decode a SQLite or D1 mapping into the shared opportunity record."""

    return LockInOpportunityRecord(
        key=LockInOpportunityKey(
            league_id=str(row["league_id"]),
            fantasy_week=int(row["fantasy_week"]),
            roster_id=int(row["roster_id"]),
            player_id=str(row["player_id"]),
            game_id=str(row["game_id"]),
        ),
        provider_player_id=str(row["provider_player_id"]),
        scheduled_start=datetime.fromisoformat(str(row["scheduled_start"])),
        action_deadline=datetime.fromisoformat(str(row["action_deadline"])),
        fantasy_week_end=datetime.fromisoformat(str(row["fantasy_week_end"])),
        status=LockInOpportunityStatus(str(row["status"])),
        next_check_at=datetime.fromisoformat(str(row["next_check_at"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        slot_index=_optional_int(row.get("slot_index")),
        slot_position=_optional_str(row.get("slot_position")),
        eligible_positions=tuple(json.loads(str(row["eligible_positions_json"]))),
        rostered_at_tipoff=_optional_bool(row.get("rostered_at_tipoff")),
        roster_evidence_at=_optional_datetime(row.get("roster_evidence_at")),
        league_configuration_fingerprint=_optional_str(row.get("league_configuration_fingerprint")),
        current_observed_score=_optional_float(row.get("current_observed_score")),
        current_observation_fingerprint=_optional_str(row.get("current_observation_fingerprint")),
        consecutive_direct_poll_count=int(row["consecutive_direct_poll_count"]),
        current_poll_id=_optional_str(row.get("current_poll_id")),
        previous_observed_at=_optional_datetime(row.get("previous_observed_at")),
        current_observed_at=_optional_datetime(row.get("current_observed_at")),
        stable_score=_optional_float(row.get("stable_score")),
        stable_fingerprint=_optional_str(row.get("stable_fingerprint")),
        stabilized_at=_optional_datetime(row.get("stabilized_at")),
        score_revision=int(row["score_revision"]),
        current_recommendation_id=_optional_str(row.get("current_recommendation_id")),
        current_recommendation_kind=_optional_str(row.get("current_recommendation_kind")),
        acknowledged_action=_optional_str(row.get("acknowledged_action")),
        acknowledged_at=_optional_datetime(row.get("acknowledged_at")),
        latest_evaluation_hash=_optional_str(row.get("latest_evaluation_hash")),
        trace_json=str(row["trace_json"]),
        row_version=int(row["row_version"]),
    )


def lock_in_key_values(key: LockInOpportunityKey) -> tuple[object, ...]:
    """Encode an opportunity primary key for shared queries."""

    return (key.league_id, key.fantasy_week, key.roster_id, key.player_id, key.game_id)


def lock_in_observation_values(
    key: LockInOpportunityKey,
    observation: LockInObservation,
    expected_version: int,
) -> tuple[object, ...]:
    """Encode a guarded observation and its repeated stabilization comparisons."""

    return (
        observation.score,
        observation.fingerprint,
        observation.fingerprint,
        observation.poll_id,
        observation.observed_at.isoformat(),
        observation.next_check_at.isoformat(),
        observation.fingerprint,
        observation.score,
        observation.fingerprint,
        observation.fingerprint,
        observation.fingerprint,
        observation.observed_at.isoformat(),
        observation.fingerprint,
        observation.fingerprint,
        observation.observed_at.isoformat(),
        *lock_in_key_values(key),
        expected_version,
        observation.poll_id,
    )


def _datetime_value(value: datetime | None) -> str | None:
    """Encode an optional timestamp using the repository ISO convention."""

    return value.isoformat() if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    """Decode an optional timestamp from SQLite or D1."""

    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_str(value: object) -> str | None:
    """Decode an optional text field."""

    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    """Decode an optional integer field."""

    return int(str(value)) if value is not None else None


def _optional_float(value: object) -> float | None:
    """Decode an optional numeric field."""

    return float(str(value)) if value is not None else None


def _optional_bool(value: object) -> bool | None:
    """Decode a nullable SQLite boolean."""

    return bool(value) if value is not None else None
