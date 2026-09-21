"""Shared bind values and row decoders for forecast SQLite and D1 repositories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastCoverage,
    ForecastFetchReceipt,
    ForecastSource,
    NormalizedForecastRevision,
    RawForecastArtifact,
)
from sleeper_manager.domain.forecast_revision_identity import forecast_semantic_hash
from sleeper_manager.persistence.forecast_codec import (
    EncodedForecastSnapshot,
    decode_forecast_snapshot,
    encode_forecast_snapshot,
)
from sleeper_manager.persistence.forecast_repository import ForecastArchiveIntegrityError


def artifact_insert_params(artifact: RawForecastArtifact) -> tuple[object, ...]:
    """Bind one raw artifact in schema column order."""

    return (
        artifact.payload_hash,
        artifact.encoding.value,
        artifact.encoded_payload,
        artifact.uncompressed_size,
        _timestamp(artifact.stored_at),
    )


def revision_insert_params(revision: NormalizedForecastRevision) -> tuple[object, ...]:
    """Encode and bind one semantic revision in schema column order."""

    snapshot = encode_forecast_snapshot(revision.records)
    return (
        revision.revision_id,
        *_source_values(revision.source),
        revision.semantic_hash,
        revision.source_payload_hash,
        _timestamp(revision.first_persisted_at),
        _optional_timestamp(revision.provider_updated_from),
        _optional_timestamp(revision.provider_updated_to),
        revision.coverage.total_rows,
        revision.coverage.numeric_forecast_rows,
        revision.coverage.core_complete_rows,
        snapshot.encoding.value,
        snapshot.encoded_records,
        snapshot.uncompressed_size,
    )


def receipt_insert_params(receipt: ForecastFetchReceipt) -> tuple[object, ...]:
    """Bind one capture receipt in schema column order."""

    return (
        receipt.receipt_id,
        *_source_values(receipt.source),
        _timestamp(receipt.scheduled_for),
        _optional_timestamp(receipt.started_at),
        _optional_timestamp(receipt.response_received_at),
        _timestamp(receipt.persisted_at),
        receipt.outcome.value,
        receipt.timing.value,
        receipt.http_status,
        receipt.payload_hash,
        receipt.semantic_hash,
        receipt.revision_id,
        receipt.error_code,
    )


def artifact_from_mapping(row: Mapping[str, Any]) -> RawForecastArtifact:
    """Decode one raw artifact row without interpreting provider bytes."""

    try:
        return RawForecastArtifact(
            payload_hash=str(row["payload_hash"]),
            encoding=ForecastArtifactEncoding(str(row["encoding"])),
            encoded_payload=_blob(row["encoded_payload"]),
            uncompressed_size=int(row["uncompressed_size"]),
            stored_at=_datetime(row["stored_at"]),
        )
    except ForecastArchiveIntegrityError:
        raise
    except (ForecastCaptureError, KeyError, TypeError, ValueError) as error:
        raise ForecastArchiveIntegrityError("Stored forecast artifact row is invalid") from error


def revision_from_mapping(row: Mapping[str, Any]) -> NormalizedForecastRevision:
    """Decode one semantic revision and verify its content-derived identity."""

    try:
        source = source_from_mapping(row)
        snapshot = EncodedForecastSnapshot(
            encoding=ForecastArtifactEncoding(str(row["records_encoding"])),
            encoded_records=_blob(row["encoded_records"]),
            uncompressed_size=int(row["records_uncompressed_size"]),
        )
        records = decode_forecast_snapshot(snapshot)
        semantic_hash = str(row["semantic_hash"])
        if forecast_semantic_hash(source, records) != semantic_hash:
            raise ForecastArchiveIntegrityError(
                "Stored forecast revision semantic hash does not match"
            )
        return NormalizedForecastRevision(
            revision_id=str(row["revision_id"]),
            source=source,
            semantic_hash=semantic_hash,
            source_payload_hash=str(row["source_payload_hash"]),
            first_persisted_at=_datetime(row["first_persisted_at"]),
            provider_updated_from=_optional_datetime(row.get("provider_updated_from")),
            provider_updated_to=_optional_datetime(row.get("provider_updated_to")),
            coverage=ForecastCoverage(
                total_rows=int(row["total_rows"]),
                numeric_forecast_rows=int(row["numeric_forecast_rows"]),
                core_complete_rows=int(row["core_complete_rows"]),
            ),
            records=records,
        )
    except ForecastArchiveIntegrityError:
        raise
    except (ForecastCaptureError, KeyError, TypeError, ValueError) as error:
        raise ForecastArchiveIntegrityError("Stored forecast revision row is invalid") from error


def receipt_from_mapping(row: Mapping[str, Any]) -> ForecastFetchReceipt:
    """Decode one receipt and reapply outcome-specific evidence validation."""

    try:
        return ForecastFetchReceipt(
            receipt_id=str(row["receipt_id"]),
            source=source_from_mapping(row),
            scheduled_for=_datetime(row["scheduled_for"]),
            started_at=_optional_datetime(row.get("started_at")),
            response_received_at=_optional_datetime(row.get("response_received_at")),
            persisted_at=_datetime(row["persisted_at"]),
            outcome=ForecastCaptureOutcome(str(row["outcome"])),
            timing=ForecastCaptureTiming(str(row["timing"])),
            http_status=(int(row["http_status"]) if row.get("http_status") is not None else None),
            payload_hash=_optional_text(row.get("payload_hash")),
            semantic_hash=_optional_text(row.get("semantic_hash")),
            revision_id=_optional_text(row.get("revision_id")),
            error_code=_optional_text(row.get("error_code")),
        )
    except ForecastArchiveIntegrityError:
        raise
    except (ForecastCaptureError, KeyError, TypeError, ValueError) as error:
        raise ForecastArchiveIntegrityError("Stored forecast receipt row is invalid") from error


def source_from_mapping(row: Mapping[str, Any]) -> ForecastSource:
    """Decode the repeated source identity stored on revisions and receipts."""

    return ForecastSource(
        provider=str(row["provider"]),
        endpoint=str(row["endpoint"]),
        season=str(row["season"]),
        season_type=str(row["season_type"]),
        horizon=str(row["horizon"]),
        adapter_version=str(row["adapter_version"]),
    )


def source_query_params(source: ForecastSource) -> tuple[object, ...]:
    """Bind a complete source identity for indexed lookup."""

    return _source_values(source)


def timestamp_query_param(value: datetime) -> str:
    """Bind a cutoff using the same sortable UTC representation as stored rows."""

    return _timestamp(value)


def _source_values(source: ForecastSource) -> tuple[object, ...]:
    """Return source fields in their shared SQL order."""

    return (
        source.provider,
        source.endpoint,
        source.season,
        source.season_type,
        source.horizon,
        source.adapter_version,
    )


def _timestamp(value: datetime) -> str:
    """Normalize stored timestamps so lexical cutoff comparison is chronological."""

    return value.astimezone(UTC).isoformat()


def _optional_timestamp(value: datetime | None) -> str | None:
    """Normalize a nullable stored timestamp."""

    return _timestamp(value) if value is not None else None


def _datetime(value: object) -> datetime:
    """Decode one required stored ISO timestamp."""

    return datetime.fromisoformat(str(value))


def _optional_datetime(value: object) -> datetime | None:
    """Decode one nullable stored ISO timestamp."""

    return None if value is None else _datetime(value)


def _optional_text(value: object) -> str | None:
    """Decode one nullable stored text field."""

    return None if value is None else str(value)


def _blob(value: object) -> bytes:
    """Normalize SQLite and D1 byte-like values."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise ForecastArchiveIntegrityError("Stored forecast payload must be bytes")


__all__ = (
    "artifact_from_mapping",
    "artifact_insert_params",
    "receipt_from_mapping",
    "receipt_insert_params",
    "revision_from_mapping",
    "revision_insert_params",
    "source_from_mapping",
    "source_query_params",
    "timestamp_query_param",
)
