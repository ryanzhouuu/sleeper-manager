"""Shared row codecs and bind values for SQLite and D1 state repositories.

Callers pass Mapping rows (D1 dicts or sqlite3.Row converted to dict). Bind helpers
keep INSERT/UPDATE parameter order aligned with `statements`. State-repository
cache reads use `cached_nba_as_of`, which marks any expired row STALE.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    CachedNBARecord,
    DataFreshnessRecord,
    DueWorkKind,
    LeagueSnapshotRecord,
    ProjectionObservationRecord,
    RecommendationRecord,
    RecommendationStatus,
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
    StoredLeagueProfile,
)

CACHE_STALE_WARNING = "Cached record has exceeded its freshness window"


def delivery_claim_id(recommendation_id: str) -> str:
    """Stable primary key for the in-flight delivery lease row."""
    return sha256(f"{recommendation_id}:delivery-claim".encode()).hexdigest()


def acknowledgement_id(recommendation_id: str, token_hash: str) -> str:
    """Deterministic acknowledgement identity for a token consumption."""
    return sha256(f"{recommendation_id}:{token_hash}".encode()).hexdigest()


def optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(str(value))


def optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def recommendation_from_mapping(row: Mapping[str, Any]) -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id=str(row["recommendation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        league_id=str(row["league_id"]),
        fantasy_week=int(row["fantasy_week"]),
        player_id=str(row["player_id"]),
        game_id=str(row["game_id"]) if row.get("game_id") is not None else None,
        decision_type=str(row["decision_type"]),
        title=str(row["title"]),
        message=str(row["message"]),
        deadline=optional_datetime(row.get("deadline")),
        policy_version=str(row["policy_version"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        status=RecommendationStatus(str(row["status"])),
        acknowledged_action=(
            AcknowledgementAction(str(row["acknowledged_action"]))
            if row.get("acknowledged_action")
            else None
        ),
        acknowledged_at=optional_datetime(row.get("acknowledged_at")),
        trace_json=str(row.get("trace_json", "{}")),
        revision=int(row.get("revision", 0)),
    )


def snapshot_from_mapping(row: Mapping[str, Any]) -> LeagueSnapshotRecord:
    return LeagueSnapshotRecord(
        snapshot_id=str(row["snapshot_id"]),
        league_id=str(row["league_id"]),
        fantasy_week=int(row["fantasy_week"]),
        payload_json=str(row["payload_json"]),
        retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
    )


def freshness_from_mapping(row: Mapping[str, Any]) -> DataFreshnessRecord:
    return DataFreshnessRecord(
        resource=str(row["resource"]),
        retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
        expires_at=optional_datetime(row.get("expires_at")),
        quality=DataQualityState(str(row["quality"])),
        warnings=tuple(json.loads(str(row["warnings_json"]))),
        errors=tuple(json.loads(str(row["errors_json"]))),
    )


def profile_from_mapping(row: Mapping[str, Any]) -> StoredLeagueProfile:
    return StoredLeagueProfile(
        league_id=str(row["league_id"]),
        fingerprint=str(row["fingerprint"]),
        retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
    )


def runtime_policy_from_mapping(row: Mapping[str, Any]) -> RuntimePolicyRecord:
    return RuntimePolicyRecord(
        version=str(row["version"]),
        payload_json=str(row["payload_json"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def cached_nba_from_mapping(row: Mapping[str, Any]) -> CachedNBARecord:
    return CachedNBARecord(
        cache_key=str(row["cache_key"]),
        provider=str(row["provider"]),
        resource=str(row["resource"]),
        schema_version=str(row["schema_version"]),
        payload_json=str(row["payload_json"]),
        retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
        source_updated_at=optional_datetime(row.get("source_updated_at")),
        expires_at=optional_datetime(row.get("expires_at")),
        quality=DataQualityState(str(row["quality"])),
        warnings=tuple(json.loads(str(row["warnings_json"]))),
        errors=tuple(json.loads(str(row["errors_json"]))),
    )


def cached_nba_as_of(record: CachedNBARecord, now: datetime) -> CachedNBARecord:
    """Mark a stored cache row stale when its freshness window has elapsed."""
    if record.expires_at is None or record.expires_at > now:
        return record
    return CachedNBARecord(
        cache_key=record.cache_key,
        provider=record.provider,
        resource=record.resource,
        schema_version=record.schema_version,
        payload_json=record.payload_json,
        retrieved_at=record.retrieved_at,
        source_updated_at=record.source_updated_at,
        expires_at=record.expires_at,
        quality=DataQualityState.STALE,
        warnings=record.warnings + (CACHE_STALE_WARNING,),
        errors=record.errors,
    )


def projection_from_mapping(row: Mapping[str, Any]) -> ProjectionObservationRecord:
    return ProjectionObservationRecord(
        history_version=str(row["history_version"]),
        player_id=str(row["player_id"]),
        game_id=str(row["game_id"]),
        game_start=datetime.fromisoformat(str(row["game_start"])),
        outcome_finalized_at=optional_datetime(row.get("outcome_finalized_at")),
        minutes=float(row["minutes"]) if row.get("minutes") is not None else None,
        started=bool(row["started"]),
        did_not_play=bool(row["did_not_play"]),
        box_score_json=str(row["box_score_json"]),
        source_version=str(row["source_version"]),
    )


def scheduled_work_from_mapping(row: Mapping[str, Any]) -> ScheduledWorkRecord:
    return ScheduledWorkRecord(
        work_id=str(row["work_id"]),
        dedupe_key=str(row["dedupe_key"]),
        kind=DueWorkKind(str(row["kind"])),
        due_at=datetime.fromisoformat(str(row["due_at"])),
        status=ScheduledWorkStatus(str(row["status"])),
        local_day=optional_str(row.get("local_day")),
        game_id=optional_str(row.get("game_id")),
        recommendation_id=optional_str(row.get("recommendation_id")),
        deadline=optional_datetime(row.get("deadline")),
        lease_expires_at=optional_datetime(row.get("lease_expires_at")),
        attempt_count=int(row["attempt_count"]),
        correlation_id=optional_str(row.get("correlation_id")),
        failure_category=optional_str(row.get("failure_category")),
        terminal_summary_json=optional_str(row.get("terminal_summary_json")),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def recommendation_insert_params(recommendation: RecommendationRecord) -> tuple[object, ...]:
    return (
        recommendation.recommendation_id,
        recommendation.idempotency_key,
        recommendation.league_id,
        recommendation.fantasy_week,
        recommendation.player_id,
        recommendation.game_id,
        recommendation.decision_type,
        recommendation.title,
        recommendation.message,
        recommendation.deadline.isoformat() if recommendation.deadline else None,
        recommendation.policy_version,
        recommendation.created_at.isoformat(),
        recommendation.status.value,
        recommendation.acknowledged_action.value if recommendation.acknowledged_action else None,
        recommendation.acknowledged_at.isoformat() if recommendation.acknowledged_at else None,
        recommendation.trace_json,
        recommendation.league_id,
        recommendation.fantasy_week,
        recommendation.decision_type,
    )


def snapshot_insert_params(snapshot: LeagueSnapshotRecord) -> tuple[object, ...]:
    return (
        snapshot.snapshot_id,
        snapshot.league_id,
        snapshot.fantasy_week,
        snapshot.payload_json,
        snapshot.retrieved_at.isoformat(),
    )


def freshness_insert_params(freshness: DataFreshnessRecord) -> tuple[object, ...]:
    return (
        freshness.resource,
        freshness.retrieved_at.isoformat(),
        freshness.expires_at.isoformat() if freshness.expires_at else None,
        freshness.quality.value,
        json.dumps(freshness.warnings),
        json.dumps(freshness.errors),
    )


def nba_cache_put_params(record: CachedNBARecord) -> tuple[object, ...]:
    return (
        record.cache_key,
        record.provider,
        record.resource,
        record.schema_version,
        record.payload_json,
        record.retrieved_at.isoformat(),
        record.source_updated_at.isoformat() if record.source_updated_at else None,
        record.expires_at.isoformat() if record.expires_at else None,
        record.quality.value,
        json.dumps(record.warnings),
        json.dumps(record.errors),
    )


def projection_observation_params(item: ProjectionObservationRecord) -> tuple[object, ...]:
    return (
        item.history_version,
        item.player_id,
        item.game_id,
        item.game_start.isoformat(),
        item.outcome_finalized_at.isoformat() if item.outcome_finalized_at else None,
        item.minutes,
        int(item.started),
        int(item.did_not_play),
        item.box_score_json,
        item.source_version,
    )


def scheduled_work_values(work: ScheduledWorkRecord) -> tuple[object, ...]:
    return (
        work.work_id,
        work.dedupe_key,
        work.kind.value,
        work.due_at.isoformat(),
        work.status.value,
        work.local_day,
        work.game_id,
        work.recommendation_id,
        work.deadline.isoformat() if work.deadline else None,
        work.lease_expires_at.isoformat() if work.lease_expires_at else None,
        work.attempt_count,
        work.correlation_id,
        work.failure_category,
        work.terminal_summary_json,
        work.created_at.isoformat(),
        work.updated_at.isoformat(),
    )


def require_terminal_scheduled_status(
    status: ScheduledWorkStatus,
    retry_at: datetime | None,
) -> None:
    """Reject finish calls that are not a completed, retry, or canceled transition."""
    if status not in {
        ScheduledWorkStatus.COMPLETED,
        ScheduledWorkStatus.RETRY,
        ScheduledWorkStatus.CANCELED,
    }:
        raise ValueError("Claimed work must finish as completed, retry, or canceled")
    if (status is ScheduledWorkStatus.RETRY) != (retry_at is not None):
        raise ValueError("Retry work requires exactly one retry timestamp")
