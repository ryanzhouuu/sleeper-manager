"""Verify projection-evaluation terminal progress and per-mode profile behavior."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sleeper_manager.backtesting.experiments import projection_evaluation_cli as cli
from sleeper_manager.backtesting.experiments.projection_evaluation import (
    ProjectionEvaluationError,
    ProjectionEvaluationOutput,
)
from sleeper_manager.backtesting.progress import (
    ProgressEvent,
    ProgressMode,
    ProgressReporter,
    ProgressStage,
    ProgressState,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_console_progress_is_immediate_and_rate_limits_advancement() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    sink = cli.ConsoleProgressSink(stream, clock=clock)
    started = ProgressEvent(
        ProgressMode.DEVELOPMENT,
        ProgressStage.BUILD_HISTORICAL_FEATURES,
        ProgressState.STARTED,
        completed=0,
        total=100,
        unit="rows",
    )
    sink(started)
    clock.value = 10
    sink(
        ProgressEvent(
            ProgressMode.DEVELOPMENT,
            ProgressStage.BUILD_HISTORICAL_FEATURES,
            ProgressState.ADVANCED,
            completed=25,
            total=100,
            unit="rows",
        )
    )
    clock.value = 30
    sink(
        ProgressEvent(
            ProgressMode.DEVELOPMENT,
            ProgressStage.BUILD_HISTORICAL_FEATURES,
            ProgressState.ADVANCED,
            completed=50,
            total=100,
            unit="rows",
            detail="row batch",
        )
    )
    clock.value = 31
    sink(
        ProgressEvent(
            ProgressMode.DEVELOPMENT,
            ProgressStage.BUILD_HISTORICAL_FEATURES,
            ProgressState.COMPLETED,
            completed=100,
            total=100,
            unit="rows",
        )
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert "started" in lines[0]
    assert "50/100 rows (50.0%)" in lines[1]
    assert "elapsed 00:30" in lines[1]
    assert "ETA 00:30" in lines[1]
    assert "row batch" in lines[1]
    assert "completed" in lines[2]


def test_console_progress_renders_fold_position_and_sanitized_failure() -> None:
    stream = io.StringIO()
    reporter = ProgressReporter(
        ProgressMode.LOCKED_RETROSPECTIVE,
        cli.ConsoleProgressSink(stream, clock=lambda: 0),
    )

    with pytest.raises(RuntimeError, match="private message"):
        with reporter.stage(
            ProgressStage.RUN_LOCKED_FOLD,
            fold_name="2025-26-early",
            fold_position=1,
            fold_count=3,
            unit="targets",
        ):
            raise RuntimeError("private message")

    output = stream.getvalue()
    assert "locked fold 1/3 2025-26-early" in output
    assert "failed (RuntimeError)" in output
    assert "private message" not in output


def _successful_runner(workspace: Path):
    def run(
        _: Path,
        *,
        league_fixture: Path,
        mode: str,
        now: datetime | None,
        progress,
    ) -> ProjectionEvaluationOutput:
        del league_fixture, now
        reporter = ProgressReporter(ProgressMode(mode), progress)
        with reporter.stage(ProgressStage.RESOLVE_SOURCE_REVISION):
            pass
        reports = workspace / "reports"
        reports.mkdir(parents=True)
        manifest = reports / "projection-evaluation-manifest.json"
        manifest.write_text(json.dumps({"source_revision": "revision", "config": 1}))
        development = reports / "projection-evaluation-development-report.json"
        development.write_text("{}")
        return ProjectionEvaluationOutput(
            manifest_path=manifest,
            development_report_path=development,
            report_json_path=None,
            report_markdown_path=None,
            dataset_version="dataset-v1",
            mode=mode,
            selected_model=None,
        )

    return run


def test_command_writes_completed_profile_and_preserves_stdout_stderr_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "run_projection_evaluation", _successful_runner(tmp_path))
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = cli.run_projection_evaluation_command(
        workspace=tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        stdout=stdout,
        stderr=stderr,
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        rss_sampler=lambda: 1.25,
    )

    profile_path = tmp_path / "profiles" / "projection-evaluation-development-latest.json"
    profile = json.loads(profile_path.read_text())
    assert result == 0
    assert "Mode: development" in stdout.getvalue()
    assert f"Performance profile: {profile_path}" in stdout.getvalue()
    assert "resolve source revision: started" in stderr.getvalue()
    assert profile["status"] == "completed"
    assert profile["source_revision"] == "revision"
    assert profile["dataset_version"] == "dataset-v1"
    assert profile["peak_rss_gib"] == 1.25
    assert len(profile["manifest_fingerprint"]) == 64


def test_command_writes_sanitized_failure_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object, progress, **__: object) -> None:
        reporter = ProgressReporter(ProgressMode.DEVELOPMENT, progress)
        with reporter.stage(ProgressStage.LOAD_RAW_INPUTS):
            raise ProjectionEvaluationError("private provider payload")

    monkeypatch.setattr(cli, "run_projection_evaluation", fail)
    stderr = io.StringIO()

    result = cli.run_projection_evaluation_command(
        workspace=tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        stdout=io.StringIO(),
        stderr=stderr,
        rss_sampler=lambda: 0.5,
    )

    profile_path = tmp_path / "profiles" / "projection-evaluation-development-latest.json"
    profile_text = profile_path.read_text()
    profile = json.loads(profile_text)
    assert result == 2
    assert profile["status"] == "failed"
    assert profile["failure_type"] == "ProjectionEvaluationError"
    assert profile["source_revision"] is None
    assert "private provider payload" not in profile_text
    assert "failed (ProjectionEvaluationError)" in stderr.getvalue()


def test_command_writes_interrupted_profile_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*_: object, progress, **__: object) -> None:
        reporter = ProgressReporter(ProgressMode.DEVELOPMENT, progress)
        with reporter.stage(ProgressStage.RUN_DEVELOPMENT_FOLD):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_projection_evaluation", interrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.run_projection_evaluation_command(
            workspace=tmp_path,
            league_fixture=tmp_path / "league.json",
            mode="development",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            rss_sampler=lambda: 0.5,
        )

    profile_path = tmp_path / "profiles" / "projection-evaluation-development-latest.json"
    profile = json.loads(profile_path.read_text())
    assert profile["status"] == "interrupted"
    assert profile["failure_type"] == "KeyboardInterrupt"
