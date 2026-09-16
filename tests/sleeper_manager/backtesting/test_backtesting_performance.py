"""Verify sanitized, atomic local performance-profile evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sleeper_manager.backtesting.performance import (
    PerformanceProfileRecorder,
    PerformanceStatus,
    _normalize_peak_rss,
    build_performance_profile,
    manifest_fingerprint,
    performance_profile_path,
    write_performance_profile,
)
from sleeper_manager.backtesting.progress import (
    ProgressEvent,
    ProgressMode,
    ProgressStage,
    ProgressState,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def progress_event(
    state: ProgressState,
    *,
    completed: int | None = None,
    total: int | None = None,
    detail: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        mode=ProgressMode.DEVELOPMENT,
        stage=ProgressStage.BUILD_HISTORICAL_FEATURES,
        state=state,
        completed=completed,
        total=total,
        unit="rows" if completed is not None or total is not None else None,
        detail=detail,
    )


def test_recorder_aggregates_stage_timing_and_omits_detail() -> None:
    clock = FakeClock()
    recorder = PerformanceProfileRecorder(
        clock=clock,
        generated_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )
    recorder(progress_event(ProgressState.STARTED))
    clock.value = 2.5
    recorder(progress_event(ProgressState.ADVANCED, completed=1, total=2, detail="private"))
    clock.value = 5.0
    recorder(progress_event(ProgressState.COMPLETED, completed=2, total=2))
    clock.value = 8.0

    profile = build_performance_profile(
        recorder,
        mode=ProgressMode.DEVELOPMENT,
        status=PerformanceStatus.COMPLETED,
        source_revision="revision",
        dataset_version="dataset",
        manifest_fingerprint="fingerprint",
        python_version="3.12.test",
        platform_name="test-platform",
        maximum_rss_gib=1.25,
    )

    assert profile.total_elapsed_seconds == 8.0
    assert profile.peak_rss_gib == 1.25
    assert profile.stages[0].elapsed_seconds == 5.0
    assert profile.stages[0].completed == 2
    assert "private" not in repr(profile)


def test_recorder_rejects_events_without_a_start_or_after_completion() -> None:
    recorder = PerformanceProfileRecorder()
    with pytest.raises(ValueError, match="before"):
        recorder(progress_event(ProgressState.ADVANCED, completed=1, total=1))

    recorder(progress_event(ProgressState.STARTED))
    recorder(progress_event(ProgressState.COMPLETED))
    with pytest.raises(ValueError, match="terminal"):
        recorder(progress_event(ProgressState.COMPLETED))


def test_failed_profile_accepts_missing_identities_but_not_private_message() -> None:
    recorder = PerformanceProfileRecorder()
    profile = build_performance_profile(
        recorder,
        mode=ProgressMode.DEVELOPMENT,
        status=PerformanceStatus.FAILED,
        failure_type="RuntimeError",
        maximum_rss_gib=0.5,
    )

    assert profile.source_revision is None
    assert profile.failure_type == "RuntimeError"
    with pytest.raises(ValueError, match="sanitized"):
        build_performance_profile(
            recorder,
            mode=ProgressMode.DEVELOPMENT,
            status=PerformanceStatus.FAILED,
            failure_type="RuntimeError: secret URL",
            maximum_rss_gib=0.5,
        )


def test_completed_profile_requires_all_modeled_identities() -> None:
    with pytest.raises(ValueError, match="identities"):
        build_performance_profile(
            PerformanceProfileRecorder(),
            mode=ProgressMode.DEVELOPMENT,
            status=PerformanceStatus.COMPLETED,
            maximum_rss_gib=0.5,
        )


def test_profile_path_uses_stable_per_mode_latest_name(tmp_path: Path) -> None:
    assert performance_profile_path(tmp_path, ProgressMode.DEVELOPMENT) == (
        tmp_path / "profiles" / "projection-evaluation-development-latest.json"
    )
    assert performance_profile_path(tmp_path, ProgressMode.LOCKED_RETROSPECTIVE) == (
        tmp_path / "profiles" / "projection-evaluation-locked-retrospective-latest.json"
    )


def test_profile_write_atomically_replaces_previous_payload(tmp_path: Path) -> None:
    recorder = PerformanceProfileRecorder(generated_at=datetime(2026, 8, 31, 12, tzinfo=UTC))
    profile = build_performance_profile(
        recorder,
        mode=ProgressMode.DEVELOPMENT,
        status=PerformanceStatus.FAILED,
        failure_type="ValueError",
        python_version="3.12.test",
        platform_name="test-platform",
        maximum_rss_gib=0.5,
    )
    path = performance_profile_path(tmp_path, ProgressMode.DEVELOPMENT)
    path.parent.mkdir(parents=True)
    path.write_text("stale")

    digest = write_performance_profile(path, profile)
    payload = json.loads(path.read_text())

    assert len(digest) == 64
    assert payload["profile_version"] == "projection-evaluation-performance-v1"
    assert payload["failure_type"] == "ValueError"
    assert not tuple(path.parent.glob(f".{path.name}.*"))


def test_manifest_fingerprint_is_canonical() -> None:
    assert manifest_fingerprint({"b": 2, "a": 1}) == manifest_fingerprint({"a": 1, "b": 2})
    assert manifest_fingerprint({"a": 1}) != manifest_fingerprint({"a": 2})


def test_peak_rss_normalizes_darwin_bytes_and_linux_kibibytes() -> None:
    assert _normalize_peak_rss(1024**3, "Darwin") == 1.0
    assert _normalize_peak_rss(1024**2, "Linux") == 1.0
