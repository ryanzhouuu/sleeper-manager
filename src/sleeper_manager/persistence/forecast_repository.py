"""Repository contracts and validation for immutable forecast archive captures."""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastFetchReceipt,
    ForecastSource,
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


@dataclass(frozen=True, slots=True)
class ForecastArchiveSelection:
    """Pairs one cutoff-legal receipt with its verified semantic revision."""

    receipt: ForecastFetchReceipt
    revision: NormalizedForecastRevision

    def __post_init__(self) -> None:
        """Reject a joined receipt and revision that disagree on identity."""

        if self.receipt.outcome not in (
            ForecastCaptureOutcome.CHANGED,
            ForecastCaptureOutcome.UNCHANGED,
        ):
            raise ForecastArchiveIntegrityError("Forecast selection receipt is not usable")
        if (
            self.receipt.source != self.revision.source
            or self.receipt.revision_id != self.revision.revision_id
            or self.receipt.semantic_hash != self.revision.semantic_hash
        ):
            raise ForecastArchiveIntegrityError(
                "Forecast selection receipt and revision identities disagree"
            )
        if self.revision.first_persisted_at > self.receipt.persisted_at:
            raise ForecastArchiveIntegrityError(
                "Forecast selection predates its revision persistence"
            )


@dataclass(frozen=True, slots=True)
class ForecastArchiveStorage:
    """Measures logical archive rows and encoded versus decoded payload bytes."""

    artifact_count: int
    revision_count: int
    receipt_count: int
    raw_encoded_bytes: int
    raw_uncompressed_bytes: int
    revision_encoded_bytes: int
    revision_uncompressed_bytes: int

    def __post_init__(self) -> None:
        """Reject impossible storage metrics returned by a backend."""

        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.artifact_count,
                self.revision_count,
                self.receipt_count,
                self.raw_encoded_bytes,
                self.raw_uncompressed_bytes,
                self.revision_encoded_bytes,
                self.revision_uncompressed_bytes,
            )
        ):
            raise ForecastArchiveIntegrityError("Forecast archive storage metrics are invalid")


class ForecastArchiveRepository(Protocol):
    """Synchronous local archive boundary implemented by SQLite."""

    def initialize(self) -> None: ...

    def save_capture(self, capture: ForecastCaptureWrite) -> ForecastArchiveWriteResult: ...

    def load_artifact(self, payload_hash: str) -> RawForecastArtifact | None: ...

    def load_revision(self, revision_id: str) -> NormalizedForecastRevision | None: ...

    def load_receipt(self, receipt_id: str) -> ForecastFetchReceipt | None: ...

    def load_latest_receipt(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastFetchReceipt | None: ...

    def load_revision_at_cutoff(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastArchiveSelection | None: ...

    def measure_storage(self) -> ForecastArchiveStorage: ...


class AsyncForecastArchiveRepository(Protocol):
    """Asynchronous forecast archive boundary implemented by Cloudflare D1."""

    async def initialize(self) -> None: ...

    async def save_capture(self, capture: ForecastCaptureWrite) -> ForecastArchiveWriteResult: ...

    async def load_artifact(self, payload_hash: str) -> RawForecastArtifact | None: ...

    async def load_revision(self, revision_id: str) -> NormalizedForecastRevision | None: ...

    async def load_receipt(self, receipt_id: str) -> ForecastFetchReceipt | None: ...

    async def load_latest_receipt(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastFetchReceipt | None: ...

    async def load_revision_at_cutoff(
        self,
        source: ForecastSource,
        *,
        cutoff: datetime,
    ) -> ForecastArchiveSelection | None: ...

    async def measure_storage(self) -> ForecastArchiveStorage: ...


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


def same_raw_artifact_content(
    stored: RawForecastArtifact,
    candidate: RawForecastArtifact,
) -> bool:
    """Compare verified source bytes while allowing different storage timestamps."""

    if stored.encoding != candidate.encoding:
        return False
    return verified_raw_payload(stored) == verified_raw_payload(candidate)


def verified_raw_payload(artifact: RawForecastArtifact) -> bytes:
    """Decode and verify one stored raw artifact's content identity."""

    try:
        payload = gzip.decompress(artifact.encoded_payload)
    except (EOFError, OSError) as error:
        raise ForecastArchiveIntegrityError("Stored forecast artifact is corrupt") from error
    if (
        len(payload) != artifact.uncompressed_size
        or sha256(payload).hexdigest() != artifact.payload_hash
    ):
        raise ForecastArchiveIntegrityError("Stored forecast artifact identity is corrupt")
    return payload


def require_aware_forecast_cutoff(cutoff: datetime) -> None:
    """Reject a cutoff that cannot be compared chronologically."""

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("Forecast cutoff must be timezone-aware")


__all__ = (
    "AsyncForecastArchiveRepository",
    "ForecastArchiveConflictError",
    "ForecastArchiveError",
    "ForecastArchiveIntegrityError",
    "ForecastArchiveRepository",
    "ForecastArchiveSelection",
    "ForecastArchiveStorage",
    "ForecastArchiveWriteResult",
    "ForecastCaptureWrite",
    "require_aware_forecast_cutoff",
    "same_raw_artifact_content",
    "validate_capture_outcome",
    "verified_raw_payload",
)
