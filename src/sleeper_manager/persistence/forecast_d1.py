"""Cloudflare D1 repository for immutable forecast capture evidence.

The repository targets the separate forecast archive binding. Schema setup stays
with D1 migrations; writes use guarded batches so identity conflicts roll back
the complete artifact, revision, and receipt graph.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from importlib import import_module
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastFetchReceipt,
    ForecastSource,
    NormalizedForecastRevision,
    RawForecastArtifact,
)
from sleeper_manager.persistence.forecast_d1_statements import (
    ASSERT_FORECAST_ARTIFACT_SQL,
    ASSERT_FORECAST_OUTCOME_SQL,
    ASSERT_FORECAST_RECEIPT_SQL,
    ASSERT_FORECAST_REVISION_SQL,
    LOAD_FORECAST_REVISION_BY_SEMANTIC_SQL,
)
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveConflictError,
    ForecastArchiveError,
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

_MISSING = object()


class D1ForecastArchiveRepository:
    """Persist forecast evidence through an injected Cloudflare D1 binding."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def initialize(self) -> None:
        """Leave schema management to the forecast D1 migration runner."""

        return None

    async def save_capture(self, capture: ForecastCaptureWrite) -> ForecastArchiveWriteResult:
        """Persist or reuse a complete capture in one guarded D1 transaction."""

        await self._validate_existing_capture(capture)
        statements: list[Any] = []
        artifact_index: int | None = None
        revision_index: int | None = None

        if capture.artifact is not None:
            artifact_params = artifact_insert_params(capture.artifact)
            artifact_index = len(statements)
            statements.append(self._statement(INSERT_FORECAST_ARTIFACT_SQL, artifact_params))
            statements.append(self._statement(ASSERT_FORECAST_ARTIFACT_SQL, artifact_params))

        if capture.revision is not None:
            if capture.artifact is None:
                raise ForecastArchiveError("Forecast revision requires a raw artifact")
            artifact_params = artifact_insert_params(capture.artifact)
            statements.append(
                self._statement(
                    ASSERT_FORECAST_OUTCOME_SQL,
                    (
                        *artifact_params,
                        capture.receipt.receipt_id,
                        capture.receipt.outcome.value,
                        capture.revision.revision_id,
                    ),
                )
            )
            revision_params = revision_insert_params(capture.revision)
            revision_index = len(statements)
            statements.append(self._statement(INSERT_FORECAST_REVISION_SQL, revision_params))
            statements.append(self._statement(ASSERT_FORECAST_REVISION_SQL, revision_params))

        receipt_params = receipt_insert_params(capture.receipt)
        receipt_index = len(statements)
        statements.append(self._statement(INSERT_FORECAST_RECEIPT_SQL, receipt_params))
        statements.append(self._statement(ASSERT_FORECAST_RECEIPT_SQL, receipt_params))

        try:
            results = await self._batch(statements)
        except Exception as error:
            try:
                await self._validate_existing_capture(capture)
            except (ForecastArchiveConflictError, ForecastArchiveError) as known:
                raise known from error
            raise

        result = ForecastArchiveWriteResult(
            artifact_created=(
                artifact_index is not None and self._changes(results[artifact_index]) == 1
            ),
            revision_created=(
                revision_index is not None and self._changes(results[revision_index]) == 1
            ),
            receipt_created=self._changes(results[receipt_index]) == 1,
        )
        validate_capture_outcome(capture, result)
        return result

    async def load_artifact(self, payload_hash: str) -> RawForecastArtifact | None:
        """Load and verify one exact provider response by content hash."""

        row = await self._first(LOAD_FORECAST_ARTIFACT_SQL, payload_hash)
        if row is None:
            return None
        artifact = artifact_from_mapping(row)
        verified_raw_payload(artifact)
        return artifact

    async def load_revision(self, revision_id: str) -> NormalizedForecastRevision | None:
        """Load and verify one normalized semantic snapshot by identity."""

        row = await self._first(LOAD_FORECAST_REVISION_SQL, revision_id)
        return revision_from_mapping(row) if row is not None else None

    async def load_receipt(self, receipt_id: str) -> ForecastFetchReceipt | None:
        """Load one capture attempt by its immutable identity."""

        row = await self._first(LOAD_FORECAST_RECEIPT_SQL, receipt_id)
        return receipt_from_mapping(row) if row is not None else None

    async def load_latest_receipt(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastFetchReceipt | None:
        """Return the newest attempt visible by a decision cutoff, including gaps."""

        require_aware_forecast_cutoff(cutoff)
        row = await self._first(
            LOAD_LATEST_FORECAST_RECEIPT_SQL,
            *source_query_params(source),
            timestamp_query_param(cutoff),
        )
        return receipt_from_mapping(row) if row is not None else None

    async def load_revision_at_cutoff(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastArchiveSelection | None:
        """Select the newest usable revision that was persisted by the cutoff."""

        require_aware_forecast_cutoff(cutoff)
        receipt_row = await self._first(
            LOAD_LATEST_USABLE_FORECAST_RECEIPT_SQL,
            *source_query_params(source),
            timestamp_query_param(cutoff),
        )
        if receipt_row is None:
            return None
        receipt = receipt_from_mapping(receipt_row)
        if receipt.revision_id is None:
            raise ForecastArchiveIntegrityError(
                "Stored usable forecast receipt has no revision identity"
            )
        revision = await self.load_revision(receipt.revision_id)
        if revision is None:
            raise ForecastArchiveIntegrityError("Stored forecast receipt revision is missing")
        return ForecastArchiveSelection(receipt, revision)

    async def measure_storage(self) -> ForecastArchiveStorage:
        """Measure logical rows and encoded versus decoded payload volume."""

        row = await self._first(MEASURE_FORECAST_STORAGE_SQL)
        if row is None:
            raise ForecastArchiveIntegrityError("Forecast storage measurement returned no row")
        try:
            return ForecastArchiveStorage(
                artifact_count=int(row["artifact_count"]),
                revision_count=int(row["revision_count"]),
                receipt_count=int(row["receipt_count"]),
                raw_encoded_bytes=int(row["raw_encoded_bytes"]),
                raw_uncompressed_bytes=int(row["raw_uncompressed_bytes"]),
                revision_encoded_bytes=int(row["revision_encoded_bytes"]),
                revision_uncompressed_bytes=int(row["revision_uncompressed_bytes"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ForecastArchiveIntegrityError(
                "Forecast storage measurement row is invalid"
            ) from error

    async def _load_revision_by_semantic(
        self,
        revision: NormalizedForecastRevision,
    ) -> NormalizedForecastRevision | None:
        """Load the unique source-scoped semantic identity for conflict checks."""

        row = await self._first(
            LOAD_FORECAST_REVISION_BY_SEMANTIC_SQL,
            *source_query_params(revision.source),
            revision.semantic_hash,
        )
        return revision_from_mapping(row) if row is not None else None

    async def _validate_existing_capture(self, capture: ForecastCaptureWrite) -> None:
        """Diagnose immutable conflicts and corrupted evidence before or after a batch."""

        existing_receipt = await self.load_receipt(capture.receipt.receipt_id)
        if existing_receipt is not None and existing_receipt != capture.receipt:
            raise ForecastArchiveConflictError(
                "Forecast receipt ID already names a different capture attempt"
            )

        if capture.artifact is not None:
            existing_artifact = await self.load_artifact(capture.artifact.payload_hash)
            if existing_artifact is not None and not same_raw_artifact_content(
                existing_artifact,
                capture.artifact,
            ):
                raise ForecastArchiveConflictError(
                    "Forecast raw artifact hash already names different content"
                )

        existing_revision: NormalizedForecastRevision | None = None
        if capture.revision is not None:
            by_id = await self.load_revision(capture.revision.revision_id)
            if by_id is not None and (
                by_id.source != capture.revision.source
                or by_id.semantic_hash != capture.revision.semantic_hash
            ):
                raise ForecastArchiveConflictError(
                    "Forecast revision ID already names different semantic content"
                )
            existing_revision = await self._load_revision_by_semantic(capture.revision)
            if (
                existing_revision is not None
                and existing_revision.revision_id != capture.revision.revision_id
            ):
                raise ForecastArchiveConflictError(
                    "Forecast semantic content already uses a different revision ID"
                )

        if existing_receipt is None and capture.revision is not None:
            validate_capture_outcome(
                capture,
                ForecastArchiveWriteResult(
                    artifact_created=False,
                    revision_created=existing_revision is None,
                    receipt_created=True,
                ),
            )

    def _statement(self, query: str, params: Sequence[object] = ()) -> Any:
        """Prepare positional SQL and convert Python buffers for the Worker binding."""

        statement = self._database.prepare(query)
        converted = tuple(_d1_bind_value(param) for param in params)
        return statement.bind(*converted) if converted else statement

    async def _first(self, query: str, *params: object) -> Mapping[str, Any] | None:
        """Run one D1 lookup and normalize its optional row mapping."""

        row = await self._statement(query, params).first()
        if row is None:
            return None
        payload = _to_python(row)
        if not isinstance(payload, Mapping):
            raise ForecastArchiveError("Unexpected D1 forecast row envelope")
        return {str(key): _to_python(value) for key, value in payload.items()}

    async def _batch(self, statements: Sequence[Any]) -> tuple[object, ...]:
        """Execute and validate one atomic sequence of prepared statements."""

        original = await self._database.batch(list(statements))
        payload = _to_python(original)
        if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
            raise ForecastArchiveError("Unexpected D1 forecast batch envelope")
        results = tuple(_to_python(result) for result in payload)
        if len(results) != len(statements):
            raise ForecastArchiveError("Incomplete D1 forecast batch result")
        for result in results:
            _require_success(result)
        return results

    @staticmethod
    def _changes(result: object) -> int:
        """Read one required D1 mutation count from a successful result."""

        meta = _field(result, "meta")
        if meta is _MISSING:
            raise ForecastArchiveError("D1 forecast result has no metadata")
        changes = _field(meta, "changes")
        if changes is _MISSING:
            raise ForecastArchiveError("D1 forecast result has no change count")
        try:
            value = int(str(changes))
        except ValueError as error:
            raise ForecastArchiveError("D1 forecast change count is invalid") from error
        if value < 0:
            raise ForecastArchiveError("D1 forecast change count is invalid")
        return value


def _d1_bind_value(value: object) -> object:
    """Convert Python bytes to a D1-supported typed array inside Pyodide."""

    if not isinstance(value, bytes):
        return value
    try:
        converter = vars(import_module("pyodide.ffi")).get("to_js")
    except ImportError:
        return value
    if not callable(converter):
        return value
    return converter(value)


def _to_python(value: object) -> object:
    """Convert a Python Worker JavaScript proxy when one is supplied."""

    converter = getattr(value, "to_py", None)
    return converter() if callable(converter) else value


def _field(value: object, name: str) -> object:
    """Read one mapping or JavaScript-proxy field without trusting its shape."""

    payload = _to_python(value)
    if isinstance(payload, Mapping) and name in payload:
        return _to_python(payload[name])
    if hasattr(value, name):
        return _to_python(getattr(value, name))
    return _MISSING


def _require_success(result: object) -> None:
    """Reject unsuccessful or malformed D1 result envelopes."""

    success = _field(result, "success")
    if success is False:
        raise ForecastArchiveError("Unsuccessful D1 forecast query")
    if success is not True:
        raise ForecastArchiveError("Unexpected D1 forecast result envelope")


__all__ = ("D1ForecastArchiveRepository",)
