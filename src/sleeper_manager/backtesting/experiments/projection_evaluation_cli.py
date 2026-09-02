"""Operator-facing progress, profiling, and summary output for projection evaluation.

The library runner remains silent by default. This module is the only place that adds elapsed
time, ETA, peak RSS, terminal output, and the ignored local performance profile.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO, cast

from sleeper_manager.backtesting.experiments.projection_evaluation import (
    ProjectionEvaluationError,
    ProjectionEvaluationOutput,
    run_projection_evaluation,
)
from sleeper_manager.backtesting.performance import (
    PerformanceProfileRecorder,
    PerformanceStatus,
    build_performance_profile,
    manifest_fingerprint,
    peak_rss_gib,
    performance_profile_path,
    write_performance_profile,
)
from sleeper_manager.backtesting.progress import (
    CompositeProgressSink,
    ProgressEvent,
    ProgressMode,
    ProgressReportingError,
    ProgressStage,
    ProgressState,
)


class ConsoleProgressSink:
    """Render stage changes immediately and rate-limit ordinary advancement."""

    def __init__(
        self,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
        minimum_interval_seconds: float = 30.0,
    ) -> None:
        if minimum_interval_seconds < 0:
            raise ValueError("Progress minimum interval must be non-negative")
        self._stream = stream
        self._clock = clock
        self._minimum_interval_seconds = minimum_interval_seconds
        self._last_line_at: float | None = None
        self._stage_started_at: dict[tuple[ProgressStage, str | None], float] = {}

    def __call__(self, event: ProgressEvent) -> None:
        """Render an event when its state or elapsed interval requires a line."""
        now = self._clock()
        key = event.stage, event.fold_name
        if event.state is ProgressState.STARTED:
            if key in self._stage_started_at:
                raise ValueError("Console progress stage was started more than once")
            self._stage_started_at[key] = now
        elif key not in self._stage_started_at:
            raise ValueError("Console progress event arrived before its stage start")

        should_print = event.state is not ProgressState.ADVANCED
        if event.state is ProgressState.ADVANCED:
            should_print = (
                self._last_line_at is None
                or now - self._last_line_at >= self._minimum_interval_seconds
            )
        if not should_print:
            return
        elapsed = now - self._stage_started_at[key]
        self._stream.write(_format_progress_line(event, elapsed) + "\n")
        self._stream.flush()
        self._last_line_at = now


def run_projection_evaluation_command(
    *,
    workspace: Path,
    league_fixture: Path,
    mode: str,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    rss_sampler: Callable[[], float] = peak_rss_gib,
) -> int:
    """Run the CLI workflow, always replacing the ignored per-mode performance profile."""
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    progress_mode = ProgressMode(mode)
    recorder = PerformanceProfileRecorder(clock=clock, generated_at=now)
    console = ConsoleProgressSink(error_stream, clock=clock)
    progress = CompositeProgressSink(recorder, console)
    output: ProjectionEvaluationOutput | None = None
    try:
        output = run_projection_evaluation(
            workspace,
            league_fixture=league_fixture,
            mode=mode,
            now=now,
            progress=progress,
        )
    except KeyboardInterrupt as error:
        _write_profile(
            recorder,
            workspace=workspace,
            mode=progress_mode,
            status=PerformanceStatus.INTERRUPTED,
            failure_type=type(error).__name__,
            output=None,
            rss_sampler=rss_sampler,
        )
        raise
    except (ProjectionEvaluationError, ProgressReportingError, OSError, ValueError) as error:
        try:
            _write_profile(
                recorder,
                workspace=workspace,
                mode=progress_mode,
                status=PerformanceStatus.FAILED,
                failure_type=type(error).__name__,
                output=None,
                rss_sampler=rss_sampler,
            )
        except (OSError, ValueError) as profile_error:
            print(f"Projection performance profile failed: {profile_error}", file=error_stream)
        print(f"Projection evaluation failed: {error}", file=error_stream)
        return 2
    except BaseException as error:
        _write_profile(
            recorder,
            workspace=workspace,
            mode=progress_mode,
            status=PerformanceStatus.FAILED,
            failure_type=type(error).__name__,
            output=None,
            rss_sampler=rss_sampler,
        )
        raise

    try:
        profile_path = _write_profile(
            recorder,
            workspace=workspace,
            mode=progress_mode,
            status=PerformanceStatus.COMPLETED,
            failure_type=None,
            output=output,
            rss_sampler=rss_sampler,
        )
    except (OSError, ValueError) as error:
        print(f"Projection performance profile failed: {error}", file=error_stream)
        return 2
    _print_summary(output, profile_path, stream=output_stream)
    return 0


def _write_profile(
    recorder: PerformanceProfileRecorder,
    *,
    workspace: Path,
    mode: ProgressMode,
    status: PerformanceStatus,
    failure_type: str | None,
    output: ProjectionEvaluationOutput | None,
    rss_sampler: Callable[[], float],
) -> Path:
    """Build and atomically write one completed, failed, or interrupted profile."""
    source_revision: str | None = None
    dataset_version: str | None = None
    fingerprint: str | None = None
    if output is not None:
        manifest = _read_manifest(output.manifest_path)
        source_value = manifest.get("source_revision")
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError("Projection manifest has no source revision")
        source_revision = source_value
        dataset_version = output.dataset_version
        fingerprint = manifest_fingerprint(manifest)
    profile = build_performance_profile(
        recorder,
        mode=mode,
        status=status,
        source_revision=source_revision,
        dataset_version=dataset_version,
        manifest_fingerprint=fingerprint,
        failure_type=failure_type,
        maximum_rss_gib=rss_sampler(),
    )
    path = performance_profile_path(workspace, mode)
    write_performance_profile(path, profile)
    return path


def _read_manifest(path: Path) -> Mapping[str, Any]:
    """Read a successful run's exact manifest for profile identity fields."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Projection manifest is unreadable after evaluation") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Projection manifest payload must be an object")
    return cast(Mapping[str, Any], payload)


def _print_summary(
    output: ProjectionEvaluationOutput,
    profile_path: Path,
    *,
    stream: TextIO,
) -> None:
    """Print the stable command result summary to stdout."""
    print(f"Mode: {output.mode}", file=stream)
    print(f"Dataset: {output.dataset_version}", file=stream)
    print(f"Frozen manifest: {output.manifest_path}", file=stream)
    if output.development_report_path.exists():
        label = (
            "Development report"
            if output.mode == ProgressMode.DEVELOPMENT.value
            else "Development report (development run)"
        )
        print(f"{label}: {output.development_report_path}", file=stream)
    print(f"Development checkpoint: {output.development_checkpoint_path}", file=stream)
    if output.report_json_path is not None:
        print(f"JSON report: {output.report_json_path}", file=stream)
        print(f"Markdown report: {output.report_markdown_path}", file=stream)
        print(f"Selected baseline: {output.selected_model}", file=stream)
    print(f"Performance profile: {profile_path}", file=stream)


def _format_progress_line(event: ProgressEvent, elapsed_seconds: float) -> str:
    """Format one sanitized human-readable progress line."""
    label = _stage_label(event)
    parts = [f"[evaluate-projections] {label}"]
    if event.state is ProgressState.STARTED:
        parts.append("started")
    elif event.state is ProgressState.FAILED:
        parts.append(f"failed ({event.detail or 'error'})")
    elif event.state is ProgressState.COMPLETED:
        parts.append("completed")
    if event.completed is not None and event.total is not None:
        count = f"{event.completed:,}/{event.total:,}"
        if event.unit is not None:
            count += f" {event.unit}"
        if event.total > 0:
            count += f" ({event.completed / event.total:.1%})"
        parts.append(count)
    if event.state is ProgressState.ADVANCED and event.detail is not None:
        parts.append(event.detail)
    parts.append(f"elapsed {_format_duration(elapsed_seconds)}")
    eta = _estimate_eta(event, elapsed_seconds)
    if eta is not None:
        parts.append(f"ETA {_format_duration(eta)}")
    return ": ".join((parts[0], ", ".join(parts[1:])))


def _stage_label(event: ProgressEvent) -> str:
    """Render fold-aware labels while retaining stable stage vocabulary."""
    if event.stage is ProgressStage.RUN_DEVELOPMENT_FOLD:
        prefix = "development fold"
    elif event.stage is ProgressStage.RUN_LOCKED_FOLD:
        prefix = "locked fold"
    else:
        return event.stage.value.replace("_", " ")
    return (
        f"{prefix} {event.fold_position}/{event.fold_count} {event.fold_name}"
        if event.fold_name is not None
        else prefix
    )


def _estimate_eta(event: ProgressEvent, elapsed_seconds: float) -> float | None:
    """Estimate remaining stage time from completed units when meaningful."""
    if (
        event.completed is None
        or event.total is None
        or event.completed <= 0
        or event.completed >= event.total
        or elapsed_seconds <= 0
    ):
        return None
    return elapsed_seconds * (event.total - event.completed) / event.completed


def _format_duration(seconds: float) -> str:
    """Format non-negative elapsed time as MM:SS or HH:MM:SS."""
    whole_seconds = max(0, round(seconds))
    minutes, second = divmod(whole_seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours:02}:{minute:02}:{second:02}"
    return f"{minute:02}:{second:02}"


__all__ = (
    "ConsoleProgressSink",
    "run_projection_evaluation_command",
)
