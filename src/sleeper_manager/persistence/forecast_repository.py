"""Repository contracts and validation for immutable forecast archive captures."""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastFetchReceipt,
    NormalizedForecastRevision,
    RawForecastArtifact,
)
from sleeper_manager.domain.forecast_revision_identity import forecast_semantic_hash


class ForecastArchiveError(ForecastCaptureError):
    """Raised when forecast persistence cannot preserve archive invariants."""


class ForecastArchiveConflictError(ForecastArchiveError):
    """Raised when an immutable archive identity already names different evidence."""


class ForecastArchiveIntegrityError(ForecastArchiveError):
    """Raised when stored evidence fails decoding or content verification."""


@dataclass(frozen=True, slots=True)
class ForecastCaptureWrite:
    """Groups every immutable object required to persist one fetch receipt."""

    receipt: ForecastFetchReceipt
    artifact: RawForecastArtifact | None = None
    revision: NormalizedForecastRevision | None = None

    def __post_init__(self) -> None:
        """Require complete, internally consistent evidence before opening a transaction."""

        needs_artifact = self.receipt.payload_hash is not None
        needs_revision = self.receipt.revision_id is not None
        if needs_artifact != (self.artifact is not None):
            raise ForecastArchiveError("Forecast receipt and raw artifact presence disagree")
        if needs_revision != (self.revision is not None):
            raise ForecastArchiveError("Forecast receipt and revision presence disagree")
        if self.artifact is not None:
            _validate_artifact(self.receipt, self.artifact)
        if self.revision is not None:
            _validate_revision(self.receipt, self.revision, self.artifact)


@dataclass(frozen=True, slots=True)
class ForecastArchiveWriteResult:
    """Reports which content-addressed objects were newly inserted."""

    artifact_created: bool
    revision_created: bool
    receipt_created: bool


class ForecastArchiveRepository(Protocol):
    """Synchronous local archive boundary implemented by SQLite."""

    def initialize(self) -> None: ...

    def save_capture(self, capture: ForecastCaptureWrite) -> ForecastArchiveWriteResult: ...


def _validate_artifact(receipt: ForecastFetchReceipt, artifact: RawForecastArtifact) -> None:
    """Verify the raw hash, byte count, and receipt link before persistence."""

    if artifact.payload_hash != receipt.payload_hash:
        raise ForecastArchiveError("Forecast receipt references a different raw artifact")
    if artifact.stored_at != receipt.persisted_at:
        raise ForecastArchiveError("Forecast artifact storage time must match its receipt")
    if artifact.encoding is not ForecastArtifactEncoding.GZIP_JSON:
        raise ForecastArchiveError("Forecast artifact encoding is unsupported")
    try:
        payload = gzip.decompress(artifact.encoded_payload)
    except (EOFError, OSError) as error:
        raise ForecastArchiveError("Forecast artifact must contain valid gzip data") from error
    if len(payload) != artifact.uncompressed_size:
        raise ForecastArchiveError("Forecast artifact uncompressed size does not match")
    if sha256(payload).hexdigest() != artifact.payload_hash:
        raise ForecastArchiveError("Forecast artifact payload hash does not match")


def _validate_revision(
    receipt: ForecastFetchReceipt,
    revision: NormalizedForecastRevision,
    artifact: RawForecastArtifact | None,
) -> None:
    """Verify source, semantic identity, and raw provenance links."""

    if revision.revision_id != receipt.revision_id:
        raise ForecastArchiveError("Forecast receipt references a different revision")
    if revision.source != receipt.source:
        raise ForecastArchiveError("Forecast receipt and revision sources disagree")
    if revision.semantic_hash != receipt.semantic_hash:
        raise ForecastArchiveError("Forecast receipt and revision semantic hashes disagree")
    if artifact is None or revision.source_payload_hash != artifact.payload_hash:
        raise ForecastArchiveError("Forecast revision references a different raw artifact")
    if revision.first_persisted_at != receipt.persisted_at:
        raise ForecastArchiveError("Forecast revision persistence time must match its receipt")
    calculated = forecast_semantic_hash(revision.source, revision.records)
    if calculated != revision.semantic_hash:
        raise ForecastArchiveError("Forecast revision semantic hash does not match its records")


def validate_capture_outcome(
    capture: ForecastCaptureWrite,
    result: ForecastArchiveWriteResult,
) -> None:
    """Require a new receipt's changed outcome to match revision insertion."""

    if not result.receipt_created or capture.revision is None:
        return
    expected = (
        ForecastCaptureOutcome.CHANGED
        if result.revision_created
        else ForecastCaptureOutcome.UNCHANGED
    )
    if capture.receipt.outcome is not expected:
        raise ForecastArchiveConflictError(
            f"Forecast receipt outcome must be {expected.value!r} for this revision"
        )


__all__ = (
    "ForecastArchiveConflictError",
    "ForecastArchiveError",
    "ForecastArchiveIntegrityError",
    "ForecastArchiveRepository",
    "ForecastArchiveWriteResult",
    "ForecastCaptureWrite",
    "validate_capture_outcome",
)
