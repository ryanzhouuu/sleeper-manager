"""Atomic local SQLite repository for immutable forecast capture evidence."""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from sleeper_manager.domain.forecast_capture import RawForecastArtifact
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveConflictError,
    ForecastArchiveIntegrityError,
    ForecastArchiveWriteResult,
    ForecastCaptureWrite,
    validate_capture_outcome,
)
from sleeper_manager.persistence.forecast_rows import (
    artifact_from_mapping,
    artifact_insert_params,
    receipt_from_mapping,
    receipt_insert_params,
    revision_from_mapping,
    revision_insert_params,
)
from sleeper_manager.persistence.forecast_statements import (
    FORECAST_ARCHIVE_SCHEMA,
    INSERT_FORECAST_ARTIFACT_SQL,
    INSERT_FORECAST_RECEIPT_SQL,
    INSERT_FORECAST_REVISION_SQL,
    LOAD_FORECAST_ARTIFACT_SQL,
    LOAD_FORECAST_RECEIPT_SQL,
    LOAD_FORECAST_REVISION_SQL,
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
        if row is None or not _same_raw_payload(artifact_from_mapping(row), artifact):
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


def _same_raw_payload(stored: RawForecastArtifact, candidate: RawForecastArtifact) -> bool:
    """Compare decoded source bytes while allowing different storage timestamps."""

    if stored.encoding != candidate.encoding:
        return False
    try:
        stored_payload = gzip.decompress(stored.encoded_payload)
        candidate_payload = gzip.decompress(candidate.encoded_payload)
    except (EOFError, OSError) as error:
        raise ForecastArchiveIntegrityError("Stored forecast artifact is corrupt") from error
    if (
        len(stored_payload) != stored.uncompressed_size
        or sha256(stored_payload).hexdigest() != stored.payload_hash
    ):
        raise ForecastArchiveIntegrityError("Stored forecast artifact identity is corrupt")
    return stored_payload == candidate_payload


__all__ = ("SQLiteForecastArchiveRepository",)
