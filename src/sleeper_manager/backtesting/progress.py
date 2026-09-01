"""Typed progress events for long-running backtesting workflows.

The contract is deterministic and contains no wall-clock values. CLI rendering and local
performance evidence consume these events without entering modeled report payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol


class ProgressMode(StrEnum):
    """Supported projection-evaluation command modes."""

    DEVELOPMENT = "development"
    LOCKED_RETROSPECTIVE = "locked_retrospective"


class ProgressStage(StrEnum):
    """Stable stages shared by evaluation progress and performance artifacts."""

    RESOLVE_SOURCE_REVISION = "resolve_source_revision"
    LOAD_RAW_INPUTS = "load_raw_inputs"
    LOAD_INJURY_ARCHIVE = "load_injury_archive"
    BUILD_HISTORICAL_FEATURES = "build_historical_features"
    VALIDATE_DEVELOPMENT_CHECKPOINT = "validate_development_checkpoint"
    RUN_DEVELOPMENT_FOLD = "run_development_fold"
    RESTORE_CALIBRATED_CONTINUATION = "restore_calibrated_continuation"
    RUN_LOCKED_FOLD = "run_locked_fold"
    BUILD_REPORTS = "build_reports"
    WRITE_ARTIFACTS = "write_artifacts"


class ProgressState(StrEnum):
    """Lifecycle states emitted for each progress stage."""

    STARTED = "started"
    ADVANCED = "advanced"
    COMPLETED = "completed"
    FAILED = "failed"


class ProgressReportingError(RuntimeError):
    """Raised when a progress sink fails while handling an event."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One sanitized, deterministic progress observation."""

    mode: ProgressMode
    stage: ProgressStage
    state: ProgressState
    fold_name: str | None = None
    fold_position: int | None = None
    fold_count: int | None = None
    completed: int | None = None
    total: int | None = None
    unit: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """Reject inconsistent counts, fold metadata, and unsafe display text."""
        _validate_fold_metadata(self.fold_name, self.fold_position, self.fold_count)
        _validate_counts(self.state, self.completed, self.total, self.unit)
        _validate_text(self.unit, "Progress unit", maximum=32)
        _validate_text(self.detail, "Progress detail", maximum=160)


class ProgressSink(Protocol):
    """Consume one progress event or raise to abort the owning workflow."""

    def __call__(self, event: ProgressEvent) -> None: ...


class ProgressCounter(Protocol):
    """Advance the counter bound to one active stage and optional fold."""

    def advance(self, completed: int, total: int, *, detail: str | None = None) -> None: ...


class CompositeProgressSink:
    """Deliver each event to every sink in deterministic order."""

    def __init__(self, *sinks: ProgressSink) -> None:
        self._sinks = sinks

    def __call__(self, event: ProgressEvent) -> None:
        """Forward an event and stop immediately if any sink fails."""
        for sink in self._sinks:
            sink(event)


class ProgressReporter:
    """Create validated stage lifecycles for one evaluation mode."""

    def __init__(self, mode: ProgressMode, sink: ProgressSink | None = None) -> None:
        self._mode = mode
        self._sink = sink

    @property
    def mode(self) -> ProgressMode:
        """Return the mode stamped on every emitted event."""
        return self._mode

    def stage(
        self,
        stage: ProgressStage,
        *,
        fold_name: str | None = None,
        fold_position: int | None = None,
        fold_count: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        detail: str | None = None,
    ) -> _ProgressStage:
        """Create a single-use context that emits started and terminal events."""
        return _ProgressStage(
            self,
            stage,
            fold_name=fold_name,
            fold_position=fold_position,
            fold_count=fold_count,
            total=total,
            unit=unit,
            detail=detail,
        )

    def _emit(self, event: ProgressEvent) -> None:
        """Deliver a validated event, converting sink errors into a stable failure type."""
        if self._sink is None:
            return
        try:
            self._sink(event)
        except ProgressReportingError:
            raise
        except BaseException as error:
            raise ProgressReportingError("Progress sink failed") from error


class _ProgressStage:
    """Track monotonic work counts for one stage/fold lifecycle."""

    def __init__(
        self,
        reporter: ProgressReporter,
        stage: ProgressStage,
        *,
        fold_name: str | None,
        fold_position: int | None,
        fold_count: int | None,
        total: int | None,
        unit: str | None,
        detail: str | None,
    ) -> None:
        self._reporter = reporter
        self._stage = stage
        self._fold_name = fold_name
        self._fold_position = fold_position
        self._fold_count = fold_count
        self._completed = 0 if total is not None else None
        self._total = total
        self._unit = unit
        self._detail = detail
        self._entered = False
        self._finished = False

    def __enter__(self) -> _ProgressStage:
        """Start the stage exactly once and return its progress counter."""
        if self._entered:
            raise ValueError("Progress stages cannot be entered more than once")
        self._entered = True
        self._emit(ProgressState.STARTED, detail=self._detail)
        return self

    def advance(self, completed: int, total: int, *, detail: str | None = None) -> None:
        """Emit a monotonic work count for the active stage."""
        if not self._entered or self._finished:
            raise ValueError("Progress can advance only while its stage is active")
        if self._completed is not None and completed < self._completed:
            raise ValueError("Progress completed count cannot decrease")
        if self._total is not None and total != self._total:
            raise ValueError("Progress total cannot change within a stage")
        self._completed = completed
        self._total = total
        self._emit(ProgressState.ADVANCED, detail=detail)

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Emit completion or failure without swallowing the workload exception."""
        del traceback
        self._finished = True
        if error is None:
            self._emit(ProgressState.COMPLETED, detail=self._detail)
            return
        if isinstance(error, ProgressReportingError):
            return
        self._emit(ProgressState.FAILED, detail=type(error).__name__)

    def _emit(self, state: ProgressState, *, detail: str | None) -> None:
        """Build and deliver an event using the stage's latest counts."""
        self._reporter._emit(
            ProgressEvent(
                mode=self._reporter.mode,
                stage=self._stage,
                state=state,
                fold_name=self._fold_name,
                fold_position=self._fold_position,
                fold_count=self._fold_count,
                completed=self._completed,
                total=self._total,
                unit=self._unit,
                detail=detail,
            )
        )


def _validate_fold_metadata(
    name: str | None,
    position: int | None,
    count: int | None,
) -> None:
    """Require complete and internally valid fold metadata."""
    supplied = (name is not None, position is not None, count is not None)
    if any(supplied) and not all(supplied):
        raise ValueError("Fold name, position, and count must be supplied together")
    _validate_text(name, "Fold name", maximum=80)
    if position is not None and count is not None:
        if position <= 0 or count <= 0 or position > count:
            raise ValueError("Fold position must be within the positive fold count")


def _validate_counts(
    state: ProgressState,
    completed: int | None,
    total: int | None,
    unit: str | None,
) -> None:
    """Validate count ranges and the state-specific count contract."""
    if state is ProgressState.ADVANCED and completed is None:
        raise ValueError("Advanced progress requires a completed count")
    if completed is not None and completed < 0:
        raise ValueError("Progress completed count must be non-negative")
    if total is not None and total < 0:
        raise ValueError("Progress total must be non-negative")
    if completed is not None and total is not None and completed > total:
        raise ValueError("Progress completed count cannot exceed its total")
    if (completed is not None or total is not None) and unit is None:
        raise ValueError("Counted progress requires a unit")


def _validate_text(value: str | None, label: str, *, maximum: int) -> None:
    """Reject blank, padded, oversized, or control-character-bearing display text."""
    if value is None:
        return
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-blank and unpadded")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")


__all__ = (
    "CompositeProgressSink",
    "ProgressCounter",
    "ProgressEvent",
    "ProgressMode",
    "ProgressReporter",
    "ProgressReportingError",
    "ProgressSink",
    "ProgressStage",
    "ProgressState",
)
