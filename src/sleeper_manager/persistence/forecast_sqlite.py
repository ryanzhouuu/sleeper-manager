"""Atomic local SQLite repository for immutable forecast capture evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastFetchReceipt,
    ForecastSource,
    NormalizedForecastRevision,
    RawForecastArtifact,
)
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveConflictError,
    ForecastArchiveIntegrityError,
    ForecastArchiveSelection,
    ForecastArchiveStorage,
    ForecastArchiveWriteResult,
    ForecastCaptureWrite,
    require_aware_forecast_cutoff,
    same_raw_artifact_content,
    validate_capture_outcome,
    verified_raw_payload,
)
from sleeper_manager.persistence.forecast_rows import (
    artifact_from_mapping,
    artifact_insert_params,
    receipt_from_mapping,
    receipt_insert_params,
    revision_from_mapping,
    revision_insert_params,
    source_query_params,
    timestamp_query_param,
)
from sleeper_manager.persistence.forecast_statements import (
    FORECAST_ARCHIVE_SCHEMA,
    INSERT_FORECAST_ARTIFACT_SQL,
    INSERT_FORECAST_RECEIPT_SQL,
    INSERT_FORECAST_REVISION_SQL,
    LOAD_FORECAST_ARTIFACT_SQL,
    LOAD_FORECAST_RECEIPT_SQL,
    LOAD_FORECAST_REVISION_SQL,
    LOAD_LATEST_FORECAST_RECEIPT_SQL,
    LOAD_LATEST_USABLE_FORECAST_RECEIPT_SQL,
    MEASURE_FORECAST_STORAGE_SQL,
)


class SQLiteForecastArchiveRepository:
    """Store each local forecast capture in one all-or-nothing transaction."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        """Open one foreign-key-enforcing archive connection."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Create the forecast archive schema idempotently."""

        with self._connect() as connection:
            connection.executescript(FORECAST_ARCHIVE_SCHEMA)

    def save_capture(self, capture: ForecastCaptureWrite) -> ForecastArchiveWriteResult:
        """Persist or reuse immutable evidence and append its receipt atomically."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            artifact_created = self._save_artifact(connection, capture)
            revision_created = self._save_revision(connection, capture)
            receipt_created = self._save_receipt(connection, capture)
            result = ForecastArchiveWriteResult(
                artifact_created=artifact_created,
                revision_created=revision_created,
                receipt_created=receipt_created,
            )
            validate_capture_outcome(capture, result)
            return result

    def load_artifact(self, payload_hash: str) -> RawForecastArtifact | None:
        """Load and verify one exact provider response by content hash."""

        with self._connect() as connection:
            row = _mapping(
                connection.execute(LOAD_FORECAST_ARTIFACT_SQL, (payload_hash,)).fetchone()
            )
        if row is None:
            return None
        artifact = artifact_from_mapping(row)
        verified_raw_payload(artifact)
        return artifact

    def load_revision(self, revision_id: str) -> NormalizedForecastRevision | None:
        """Load and verify one normalized semantic snapshot by identity."""

        with self._connect() as connection:
            row = _mapping(
                connection.execute(LOAD_FORECAST_REVISION_SQL, (revision_id,)).fetchone()
            )
        return revision_from_mapping(row) if row is not None else None

    def load_receipt(self, receipt_id: str) -> ForecastFetchReceipt | None:
        """Load one capture attempt by its immutable identity."""

        with self._connect() as connection:
            row = _mapping(connection.execute(LOAD_FORECAST_RECEIPT_SQL, (receipt_id,)).fetchone())
        return receipt_from_mapping(row) if row is not None else None

    def load_latest_receipt(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastFetchReceipt | None:
        """Return the newest attempt visible by a decision cutoff, including gaps."""

        require_aware_forecast_cutoff(cutoff)
        params = (*source_query_params(source), timestamp_query_param(cutoff))
        with self._connect() as connection:
            row = _mapping(connection.execute(LOAD_LATEST_FORECAST_RECEIPT_SQL, params).fetchone())
        return receipt_from_mapping(row) if row is not None else None

    def load_revision_at_cutoff(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastArchiveSelection | None:
        """Select the newest usable revision that was persisted by the cutoff."""

        require_aware_forecast_cutoff(cutoff)
        params = (*source_query_params(source), timestamp_query_param(cutoff))
        with self._connect() as connection:
            receipt_row = _mapping(
                connection.execute(
                    LOAD_LATEST_USABLE_FORECAST_RECEIPT_SQL,
                    params,
                ).fetchone()
            )
            if receipt_row is None:
                return None
            receipt = receipt_from_mapping(receipt_row)
            if receipt.revision_id is None:
                raise ForecastArchiveIntegrityError(
                    "Stored usable forecast receipt has no revision identity"
                )
            revision_row = _mapping(
                connection.execute(
                    LOAD_FORECAST_REVISION_SQL,
                    (receipt.revision_id,),
                ).fetchone()
            )
        if revision_row is None:
            raise ForecastArchiveIntegrityError("Stored forecast receipt revision is missing")
        return ForecastArchiveSelection(receipt, revision_from_mapping(revision_row))

    def measure_storage(self) -> ForecastArchiveStorage:
        """Measure logical rows and encoded versus decoded payload volume."""

        with self._connect() as connection:
            row = _mapping(connection.execute(MEASURE_FORECAST_STORAGE_SQL).fetchone())
        if row is None:
            raise ForecastArchiveIntegrityError("Forecast storage measurement returned no row")
        return ForecastArchiveStorage(
            artifact_count=int(row["artifact_count"]),
            revision_count=int(row["revision_count"]),
            receipt_count=int(row["receipt_count"]),
            raw_encoded_bytes=int(row["raw_encoded_bytes"]),
            raw_uncompressed_bytes=int(row["raw_uncompressed_bytes"]),
            revision_encoded_bytes=int(row["revision_encoded_bytes"]),
            revision_uncompressed_bytes=int(row["revision_uncompressed_bytes"]),
        )

    def _save_artifact(
        self,
        connection: sqlite3.Connection,
        capture: ForecastCaptureWrite,
    ) -> bool:
        """Insert raw bytes once and reject a conflicting content identity."""

        artifact = capture.artifact
        if artifact is None:
            return False
        cursor = connection.execute(INSERT_FORECAST_ARTIFACT_SQL, artifact_insert_params(artifact))
        if cursor.rowcount == 1:
            return True
        row = _mapping(
            connection.execute(LOAD_FORECAST_ARTIFACT_SQL, (artifact.payload_hash,)).fetchone()
        )
        if row is None or not same_raw_artifact_content(artifact_from_mapping(row), artifact):
            raise ForecastArchiveConflictError(
                "Forecast raw artifact hash already names different content"
            )
        return False

    def _save_revision(
        self,
        connection: sqlite3.Connection,
        capture: ForecastCaptureWrite,
    ) -> bool:
        """Insert semantic content once and validate a deduplicated identity."""

        revision = capture.revision
        if revision is None:
            return False
        cursor = connection.execute(INSERT_FORECAST_REVISION_SQL, revision_insert_params(revision))
        if cursor.rowcount == 1:
            return True
        row = _mapping(
            connection.execute(LOAD_FORECAST_REVISION_SQL, (revision.revision_id,)).fetchone()
        )
        if row is None:
            raise ForecastArchiveConflictError(
                "Forecast semantic content already uses a different revision ID"
            )
        stored = revision_from_mapping(row)
        if stored.source != revision.source or stored.semantic_hash != revision.semantic_hash:
            raise ForecastArchiveConflictError(
                "Forecast revision ID already names different semantic content"
            )
        return False

    def _save_receipt(
        self,
        connection: sqlite3.Connection,
        capture: ForecastCaptureWrite,
    ) -> bool:
        """Append one attempt or accept an exact persistence retry."""

        receipt = capture.receipt
        cursor = connection.execute(INSERT_FORECAST_RECEIPT_SQL, receipt_insert_params(receipt))
        if cursor.rowcount == 1:
            return True
        row = _mapping(
            connection.execute(LOAD_FORECAST_RECEIPT_SQL, (receipt.receipt_id,)).fetchone()
        )
        if row is None or receipt_from_mapping(row) != receipt:
            raise ForecastArchiveConflictError(
                "Forecast receipt ID already names a different capture attempt"
            )
        return False


def _mapping(row: sqlite3.Row | None) -> Mapping[str, Any] | None:
    """Expose SQLite rows through the shared forecast decoder boundary."""

    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


__all__ = ("SQLiteForecastArchiveRepository",)
