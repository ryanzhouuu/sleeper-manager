"""Local performance evidence derived from deterministic backtest progress events.

Profiles are operational artifacts, not modeled evaluation output. They intentionally omit
progress details and exception messages so local paths or private provider values cannot leak.
"""

from __future__ import annotations

import platform
import resource
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.artifacts import (
    atomic_write_json,
    canonical_json_bytes,
    sha256_bytes,
)
from sleeper_manager.backtesting.progress import (
    ProgressEvent,
    ProgressMode,
    ProgressStage,
    ProgressState,
)

PROFILE_VERSION = "projection-evaluation-performance-v1"


class PerformanceStatus(StrEnum):
    """Terminal outcomes represented by a local performance profile."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class StagePerformanceRecord:
    """Aggregate timing and work counts for one stage or fold."""

    stage: ProgressStage
    state: ProgressState
    fold_name: str | None
    fold_position: int | None
    fold_count: int | None
    elapsed_seconds: float
    completed: int | None
    total: int | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class PerformanceProfile:
    """Versioned local evidence for one projection-evaluation command run."""

    profile_version: str
    status: PerformanceStatus
    mode: ProgressMode
    generated_at: datetime
    python_version: str
    platform: str
    source_revision: str | None
    dataset_version: str | None
    manifest_fingerprint: str | None
    total_elapsed_seconds: float
    peak_rss_gib: float
    stages: tuple[StagePerformanceRecord, ...]
    failure_type: str | None


@dataclass(slots=True)
class _StageTiming:
    """Mutable recorder state retained until a profile snapshot is built."""

    started_at: float
    latest_event: ProgressEvent
    elapsed_seconds: float | None = None


class PerformanceProfileRecorder:
    """Aggregate progress events into one timing record per stage/fold."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        generated_at: datetime | None = None,
    ) -> None:
        timestamp = generated_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Performance profile generated_at must be timezone-aware")
        self._clock = clock
        self._generated_at = timestamp.astimezone(UTC)
        self._started_at = clock()
        self._timings: dict[tuple[ProgressStage, str | None], _StageTiming] = {}
        self._order: list[tuple[ProgressStage, str | None]] = []

    @property
    def generated_at(self) -> datetime:
        """Return the UTC wall-clock timestamp for this run."""
        return self._generated_at

    def __call__(self, event: ProgressEvent) -> None:
        """Record one lifecycle event, rejecting missing or duplicate stage starts."""
        key = event.stage, event.fold_name
        now = self._clock()
        if event.state is ProgressState.STARTED:
            if key in self._timings:
                raise ValueError("Performance stage was started more than once")
            self._timings[key] = _StageTiming(now, event)
            self._order.append(key)
            return
        timing = self._timings.get(key)
        if timing is None:
            raise ValueError("Performance stage event arrived before its start")
        if timing.elapsed_seconds is not None:
            raise ValueError("Performance stage event arrived after its terminal state")
        timing.latest_event = event
        if event.state in (ProgressState.COMPLETED, ProgressState.FAILED):
            timing.elapsed_seconds = now - timing.started_at

    def total_elapsed_seconds(self) -> float:
        """Return monotonic elapsed time since recorder construction."""
        return self._clock() - self._started_at

    def stage_records(self) -> tuple[StagePerformanceRecord, ...]:
        """Snapshot completed and active stage timings in original start order."""
        now = self._clock()
        records: list[StagePerformanceRecord] = []
        for key in self._order:
            timing = self._timings[key]
            event = timing.latest_event
            elapsed = (
                timing.elapsed_seconds
                if timing.elapsed_seconds is not None
                else now - timing.started_at
            )
            records.append(
                StagePerformanceRecord(
                    stage=event.stage,
                    state=event.state,
                    fold_name=event.fold_name,
                    fold_position=event.fold_position,
                    fold_count=event.fold_count,
                    elapsed_seconds=round(elapsed, 6),
                    completed=event.completed,
                    total=event.total,
                    unit=event.unit,
                )
            )
        return tuple(records)


def build_performance_profile(
    recorder: PerformanceProfileRecorder,
    *,
    mode: ProgressMode,
    status: PerformanceStatus,
    source_revision: str | None = None,
    dataset_version: str | None = None,
    manifest_fingerprint: str | None = None,
    failure_type: str | None = None,
    python_version: str | None = None,
    platform_name: str | None = None,
    maximum_rss_gib: float | None = None,
) -> PerformanceProfile:
    """Build a sanitized profile, requiring exact identities for completed runs."""
    identities = (source_revision, dataset_version, manifest_fingerprint)
    if status is PerformanceStatus.COMPLETED and not all(identities):
        raise ValueError("Completed performance profiles require all modeled identities")
    if status is PerformanceStatus.COMPLETED and failure_type is not None:
        raise ValueError("Completed performance profiles cannot carry a failure type")
    _validate_failure_type(failure_type)
    return PerformanceProfile(
        profile_version=PROFILE_VERSION,
        status=status,
        mode=mode,
        generated_at=recorder.generated_at,
        python_version=python_version or platform.python_version(),
        platform=platform_name or platform.platform(),
        source_revision=source_revision,
        dataset_version=dataset_version,
        manifest_fingerprint=manifest_fingerprint,
        total_elapsed_seconds=round(recorder.total_elapsed_seconds(), 6),
        peak_rss_gib=round(maximum_rss_gib if maximum_rss_gib is not None else peak_rss_gib(), 6),
        stages=recorder.stage_records(),
        failure_type=failure_type,
    )


def performance_profile_path(workspace: Path, mode: ProgressMode) -> Path:
    """Return the ignored per-mode latest-profile path beneath a workspace."""
    mode_name = mode.value.replace("_", "-")
    return workspace / "profiles" / f"projection-evaluation-{mode_name}-latest.json"


def write_performance_profile(path: Path, profile: PerformanceProfile) -> str:
    """Atomically replace a local profile and return its content hash."""
    return atomic_write_json(path, profile)


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Return the canonical content fingerprint used by profiles and checkpoints."""
    return sha256_bytes(canonical_json_bytes(manifest))


def peak_rss_gib() -> float:
    """Return process peak resident memory in GiB on supported local platforms."""
    maximum_rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return _normalize_peak_rss(maximum_rss, platform.system())


def _normalize_peak_rss(maximum_rss: float, system_name: str) -> float:
    """Normalize Darwin bytes or Linux/BSD KiB into GiB."""
    if maximum_rss < 0:
        raise ValueError("Peak RSS must be non-negative")
    if system_name == "Darwin":
        return maximum_rss / (1024**3)
    return maximum_rss / (1024**2)


def _validate_failure_type(value: str | None) -> None:
    """Allow only exception class names, never exception messages."""
    if value is None:
        return
    if not value or len(value) > 100 or not value.replace("_", "").isalnum():
        raise ValueError("Performance failure_type must be a sanitized class name")


__all__ = (
    "PerformanceProfile",
    "PerformanceProfileRecorder",
    "PerformanceStatus",
    "StagePerformanceRecord",
    "build_performance_profile",
    "manifest_fingerprint",
    "peak_rss_gib",
    "performance_profile_path",
    "write_performance_profile",
)
