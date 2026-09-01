"""Pin the deterministic backtesting progress contract and failure behavior."""

from __future__ import annotations

import pytest

from sleeper_manager.backtesting.progress import (
    CompositeProgressSink,
    ProgressEvent,
    ProgressMode,
    ProgressReporter,
    ProgressReportingError,
    ProgressStage,
    ProgressState,
)


def event(**changes: object) -> ProgressEvent:
    values: dict[str, object] = {
        "mode": ProgressMode.DEVELOPMENT,
        "stage": ProgressStage.BUILD_HISTORICAL_FEATURES,
        "state": ProgressState.ADVANCED,
        "completed": 1,
        "total": 2,
        "unit": "rows",
    }
    values.update(changes)
    return ProgressEvent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    (
        {"completed": -1},
        {"completed": 3, "total": 2},
        {"unit": None},
        {"fold_name": "fold", "fold_position": 1},
        {"fold_name": "fold", "fold_position": 2, "fold_count": 1},
        {"detail": "secret\nsecond line"},
        {"detail": "x" * 161},
    ),
)
def test_progress_event_rejects_invalid_contract_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        event(**changes)


def test_reporter_emits_started_advanced_and_completed_events() -> None:
    events: list[ProgressEvent] = []
    reporter = ProgressReporter(ProgressMode.DEVELOPMENT, events.append)

    with reporter.stage(
        ProgressStage.RUN_DEVELOPMENT_FOLD,
        fold_name="2024-25-middle",
        fold_position=2,
        fold_count=3,
        unit="targets",
    ) as counter:
        counter.advance(1, 2, detail="target batch")
        counter.advance(2, 2)

    assert [item.state for item in events] == [
        ProgressState.STARTED,
        ProgressState.ADVANCED,
        ProgressState.ADVANCED,
        ProgressState.COMPLETED,
    ]
    assert events[-1].completed == 2
    assert events[-1].total == 2
    assert events[-1].fold_position == 2


def test_reporter_rejects_decreasing_counts_and_changed_totals() -> None:
    reporter = ProgressReporter(ProgressMode.DEVELOPMENT)

    with reporter.stage(ProgressStage.LOAD_RAW_INPUTS, unit="resources") as counter:
        counter.advance(2, 4)
        with pytest.raises(ValueError, match="decrease"):
            counter.advance(1, 4)
        with pytest.raises(ValueError, match="total"):
            counter.advance(3, 5)


def test_reporter_emits_sanitized_failure_and_preserves_workload_error() -> None:
    events: list[ProgressEvent] = []
    reporter = ProgressReporter(ProgressMode.DEVELOPMENT, events.append)

    with pytest.raises(RuntimeError, match="private payload value"):
        with reporter.stage(ProgressStage.LOAD_INJURY_ARCHIVE):
            raise RuntimeError("private payload value")

    assert events[-1].state is ProgressState.FAILED
    assert events[-1].detail == "RuntimeError"
    assert "private payload value" not in repr(events[-1])


def test_sink_failure_aborts_without_retrying_failed_event() -> None:
    received: list[ProgressEvent] = []

    def failing_sink(item: ProgressEvent) -> None:
        received.append(item)
        raise OSError("terminal closed")

    reporter = ProgressReporter(ProgressMode.DEVELOPMENT, failing_sink)

    with pytest.raises(ProgressReportingError, match="Progress sink failed"):
        with reporter.stage(ProgressStage.RESOLVE_SOURCE_REVISION):
            pass

    assert len(received) == 1
    assert received[0].state is ProgressState.STARTED


def test_composite_sink_preserves_delivery_order() -> None:
    calls: list[str] = []
    item = event()
    sink = CompositeProgressSink(
        lambda _: calls.append("first"),
        lambda _: calls.append("second"),
    )

    sink(item)

    assert calls == ["first", "second"]
