"""Cloudflare D1 binding adapter for immutable forecast capture evidence.

The repository targets the separate forecast archive binding. Schema setup stays
with D1 migrations, while backend-neutral row codecs verify loaded evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any

from sleeper_manager.domain.forecast_capture import (
    ForecastFetchReceipt,
    NormalizedForecastRevision,
    RawForecastArtifact,
)
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveError,
    verified_raw_payload,
)
from sleeper_manager.persistence.forecast_rows import (
    artifact_from_mapping,
    receipt_from_mapping,
    revision_from_mapping,
)
from sleeper_manager.persistence.forecast_statements import (
    LOAD_FORECAST_ARTIFACT_SQL,
    LOAD_FORECAST_RECEIPT_SQL,
    LOAD_FORECAST_REVISION_SQL,
)


class D1ForecastArchiveRepository:
    """Load forecast evidence through an injected Cloudflare D1 binding."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def initialize(self) -> None:
        """Leave schema management to the forecast D1 migration runner."""

        return None

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


__all__ = ("D1ForecastArchiveRepository",)
