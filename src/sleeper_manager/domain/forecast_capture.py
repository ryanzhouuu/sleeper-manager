"""Provider-neutral contracts for prospective forecast evidence.

These immutable types separate capture evidence from model qualification. Provider
adapters create them, persistence stores them, and cutoff readers return them without
deciding whether a forecast is suitable for advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite

from sleeper_manager.domain._forecast_capture_validation import (
    ForecastCaptureError,
    require_aware,
    require_sha256,
    require_text,
)


class ForecastArtifactEncoding(StrEnum):
    """Supported encodings for immutable source payloads."""

    GZIP_JSON = "gzip_json"


class ForecastCaptureOutcome(StrEnum):
    """Semantic outcome of one scheduled capture attempt."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    INVALID = "invalid"
    SUPPRESSED = "suppressed"


class ForecastCaptureTiming(StrEnum):
    """Whether the attempt completed inside its intended capture window."""

    ON_TIME = "on_time"
    LATE = "late"


class ForecastRetrievalStatus(StrEnum):
    """Result category for a point-in-time player forecast lookup."""

    AVAILABLE = "available"
    MISSING = "missing"
    INVALID = "invalid"
    STALE = "stale"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class ForecastSource:
    """Identifies the provider contract that produced a forecast payload."""

    provider: str
    endpoint: str
    season: str
    season_type: str
    horizon: str
    adapter_version: str

    def __post_init__(self) -> None:
        for label, value in (
            ("provider", self.provider),
            ("endpoint", self.endpoint),
            ("season", self.season),
            ("season type", self.season_type),
            ("horizon", self.horizon),
            ("adapter version", self.adapter_version),
        ):
            require_text(value, f"Forecast source {label}")


@dataclass(frozen=True, slots=True)
class NormalizedPlayerForecast:
    """Preserves supplied numeric fields without assigning undocumented semantics."""

    player_id: str
    company: str
    stats: tuple[tuple[str, float], ...]
    team_id: str | None = None
    provider_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.player_id, "Forecast player ID")
        require_text(self.company, "Forecast company")
        if self.team_id is not None:
            require_text(self.team_id, "Forecast team ID")
        if self.provider_updated_at is not None:
            require_aware(self.provider_updated_at, "Forecast provider update")

        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for name, value in self.stats:
            require_text(name, "Forecast stat name")
            if name in seen:
                raise ForecastCaptureError(f"Forecast stat {name!r} is duplicated")
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ForecastCaptureError(f"Forecast stat {name!r} must be a finite number")
            seen.add(name)
            normalized.append((name, float(value)))
        object.__setattr__(self, "stats", tuple(sorted(normalized)))

    def stat(self, name: str) -> float | None:
        """Return a supplied value, preserving absence as `None`."""

        return next((value for key, value in self.stats if key == name), None)


@dataclass(frozen=True, slots=True)
class ForecastCoverage:
    """Summarizes source rows without treating incomplete forecasts as absent players."""

    total_rows: int
    numeric_forecast_rows: int
    core_complete_rows: int

    def __post_init__(self) -> None:
        values = (self.total_rows, self.numeric_forecast_rows, self.core_complete_rows)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ForecastCaptureError("Forecast coverage counts must be integers")
        if not 0 <= self.core_complete_rows <= self.numeric_forecast_rows <= self.total_rows:
            raise ForecastCaptureError("Forecast coverage counts are inconsistent")


@dataclass(frozen=True, slots=True)
class RawForecastArtifact:
    """Stores one immutable encoded provider response under its raw-content hash."""

    payload_hash: str
    encoding: ForecastArtifactEncoding
    encoded_payload: bytes
    uncompressed_size: int
    stored_at: datetime

    def __post_init__(self) -> None:
        require_sha256(self.payload_hash, "Raw forecast payload hash")
        if not self.encoded_payload:
            raise ForecastCaptureError("Raw forecast artifact payload must not be empty")
        if (
            isinstance(self.uncompressed_size, bool)
            or not isinstance(self.uncompressed_size, int)
            or self.uncompressed_size <= 0
        ):
            raise ForecastCaptureError("Raw forecast artifact size must be positive")
        require_aware(self.stored_at, "Raw forecast artifact storage time")


@dataclass(frozen=True, slots=True)
class NormalizedForecastRevision:
    """Represents one deduplicated semantic forecast surface."""

    revision_id: str
    source: ForecastSource
    semantic_hash: str
    source_payload_hash: str
    first_persisted_at: datetime
    coverage: ForecastCoverage
    records: tuple[NormalizedPlayerForecast, ...]
    provider_updated_from: datetime | None = None
    provider_updated_to: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.revision_id, "Forecast revision ID")
        require_sha256(self.semantic_hash, "Forecast semantic hash")
        require_sha256(self.source_payload_hash, "Forecast source payload hash")
        require_aware(self.first_persisted_at, "Forecast revision persistence time")
        for label, value in (
            ("earliest provider update", self.provider_updated_from),
            ("latest provider update", self.provider_updated_to),
        ):
            if value is not None:
                require_aware(value, label)
        if (
            self.provider_updated_from is not None
            and self.provider_updated_to is not None
            and self.provider_updated_from > self.provider_updated_to
        ):
            raise ForecastCaptureError("Forecast provider update range is inverted")

        ordered = tuple(sorted(self.records, key=lambda item: item.player_id))
        player_ids = tuple(record.player_id for record in ordered)
        if len(set(player_ids)) != len(player_ids):
            raise ForecastCaptureError("Forecast revision contains duplicate player IDs")
        if self.coverage.total_rows != len(ordered):
            raise ForecastCaptureError("Forecast coverage total must match normalized records")
        object.__setattr__(self, "records", ordered)


@dataclass(frozen=True, slots=True)
class ForecastFetchReceipt:
    """Records one due capture even when no usable revision was produced."""

    receipt_id: str
    source: ForecastSource
    scheduled_for: datetime
    persisted_at: datetime
    outcome: ForecastCaptureOutcome
    timing: ForecastCaptureTiming
    started_at: datetime | None = None
    response_received_at: datetime | None = None
    http_status: int | None = None
    payload_hash: str | None = None
    semantic_hash: str | None = None
    revision_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        require_text(self.receipt_id, "Forecast receipt ID")
        require_aware(self.scheduled_for, "Forecast scheduled time")
        require_aware(self.persisted_at, "Forecast receipt persistence time")
        if self.persisted_at < self.scheduled_for:
            raise ForecastCaptureError("Forecast receipt was persisted before it was scheduled")
        for label, value in (
            ("Forecast request start", self.started_at),
            ("Forecast response receipt", self.response_received_at),
        ):
            if value is not None:
                require_aware(value, label)
        if self.started_at is not None and self.started_at > self.persisted_at:
            raise ForecastCaptureError("Forecast receipt was persisted before its request started")
        if self.response_received_at is not None:
            if self.started_at is None or self.response_received_at < self.started_at:
                raise ForecastCaptureError("Forecast response precedes its request")
            if self.response_received_at > self.persisted_at:
                raise ForecastCaptureError("Forecast response was persisted before it arrived")
        if self.http_status is not None:
            if (
                isinstance(self.http_status, bool)
                or not isinstance(self.http_status, int)
                or not 100 <= self.http_status <= 599
            ):
                raise ForecastCaptureError("Forecast HTTP status must be between 100 and 599")
            if self.response_received_at is None:
                raise ForecastCaptureError("Forecast HTTP status requires a received response")
        if self.payload_hash is not None:
            require_sha256(self.payload_hash, "Forecast receipt payload hash")
            if self.response_received_at is None:
                raise ForecastCaptureError("Forecast payload hash requires a received response")
        if self.semantic_hash is not None:
            require_sha256(self.semantic_hash, "Forecast receipt semantic hash")
        if self.revision_id is not None:
            require_text(self.revision_id, "Forecast receipt revision ID")
        if self.error_code is not None:
            require_text(self.error_code, "Forecast receipt error code")
        self._validate_outcome()

    def _validate_outcome(self) -> None:
        """Enforce which evidence exists for each capture outcome."""

        usable = self.outcome in (
            ForecastCaptureOutcome.CHANGED,
            ForecastCaptureOutcome.UNCHANGED,
        )
        if usable:
            if (
                self.started_at is None
                or self.response_received_at is None
                or self.http_status is None
                or not 200 <= self.http_status <= 299
                or self.payload_hash is None
                or self.semantic_hash is None
                or self.revision_id is None
                or self.error_code is not None
            ):
                raise ForecastCaptureError("Usable forecast receipts require complete evidence")
            return
        if self.outcome is ForecastCaptureOutcome.INVALID:
            if (
                self.started_at is None
                or self.response_received_at is None
                or self.http_status is None
                or not 200 <= self.http_status <= 299
                or self.payload_hash is None
                or self.semantic_hash is not None
                or self.revision_id is not None
                or self.error_code is None
            ):
                raise ForecastCaptureError("Invalid forecast receipts require raw failure evidence")
            return
        if self.outcome is ForecastCaptureOutcome.FAILED:
            if (
                self.started_at is None
                or self.semantic_hash is not None
                or self.revision_id is not None
                or self.error_code is None
            ):
                raise ForecastCaptureError(
                    "Failed forecast receipts require an error without revision"
                )
            return
        if (
            any(
                value is not None
                for value in (
                    self.started_at,
                    self.response_received_at,
                    self.http_status,
                    self.payload_hash,
                    self.semantic_hash,
                    self.revision_id,
                )
            )
            or self.error_code is None
        ):
            raise ForecastCaptureError(
                "Suppressed forecast receipts cannot contain request evidence"
            )


@dataclass(frozen=True, slots=True)
class ForecastRevisionProvenance:
    """Binds a retrieved revision to the observation that made it available."""

    receipt_id: str
    revision_id: str
    source: ForecastSource
    payload_hash: str
    semantic_hash: str
    persisted_at: datetime

    def __post_init__(self) -> None:
        require_text(self.receipt_id, "Forecast provenance receipt ID")
        require_text(self.revision_id, "Forecast provenance revision ID")
        require_sha256(self.payload_hash, "Forecast provenance payload hash")
        require_sha256(self.semantic_hash, "Forecast provenance semantic hash")
        require_aware(self.persisted_at, "Forecast provenance persistence time")


@dataclass(frozen=True, slots=True)
class ForecastRetrievalResult:
    """Returns cutoff-safe forecast evidence or an explicit reason it is unavailable."""

    status: ForecastRetrievalStatus
    player_id: str
    cutoff: datetime
    detail: str
    forecast: NormalizedPlayerForecast | None = None
    provenance: ForecastRevisionProvenance | None = None
    evidence_receipt_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.player_id, "Forecast retrieval player ID")
        require_aware(self.cutoff, "Forecast retrieval cutoff")
        require_text(self.detail, "Forecast retrieval detail")
        if self.evidence_receipt_id is not None:
            require_text(self.evidence_receipt_id, "Forecast retrieval evidence receipt ID")

        if self.status in (ForecastRetrievalStatus.AVAILABLE, ForecastRetrievalStatus.STALE):
            if self.forecast is None or self.provenance is None:
                raise ForecastCaptureError(
                    "Available or stale retrievals require forecast provenance"
                )
            if self.forecast.player_id != self.player_id:
                raise ForecastCaptureError("Retrieved forecast belongs to a different player")
        elif self.status is ForecastRetrievalStatus.MISSING:
            if self.forecast is not None or self.provenance is None:
                raise ForecastCaptureError("Missing retrievals require revision provenance only")
        elif (
            self.forecast is not None
            or self.provenance is not None
            or self.evidence_receipt_id is None
        ):
            raise ForecastCaptureError("Invalid or gap retrievals require receipt evidence only")

        if self.provenance is not None and self.provenance.persisted_at > self.cutoff:
            raise ForecastCaptureError("Forecast retrieval cannot use post-cutoff evidence")

    @property
    def age(self) -> timedelta | None:
        """Measure revision age at the cutoff when provenance is available."""

        if self.provenance is None:
            return None
        return self.cutoff - self.provenance.persisted_at


__all__ = (
    "ForecastArtifactEncoding",
    "ForecastCaptureError",
    "ForecastCaptureOutcome",
    "ForecastCaptureTiming",
    "ForecastCoverage",
    "ForecastFetchReceipt",
    "ForecastRetrievalResult",
    "ForecastRetrievalStatus",
    "ForecastRevisionProvenance",
    "ForecastSource",
    "NormalizedForecastRevision",
    "NormalizedPlayerForecast",
    "RawForecastArtifact",
)
