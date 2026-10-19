"""Stage logs begin before execution and never expose exception messages or private payloads."""

import json

import pytest

from sleeper_manager.cloudflare.stage_logging import timed_stage


def test_stage_entry_is_visible_before_work_and_reports_wall_time(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clock = iter((1.0, 1.125))
    monkeypatch.setattr(
        "sleeper_manager.cloudflare.stage_logging.perf_counter", lambda: next(clock)
    )
    with timed_stage("planning", correlation_id="wake-1"):
        entry = json.loads(capsys.readouterr().out)
        assert entry == {
            "event": "scheduled_stage",
            "stage": "planning",
            "outcome": "started",
            "correlation_id": "wake-1",
        }
    completed = json.loads(capsys.readouterr().out)
    assert completed["outcome"] == "completed"
    assert completed["wall_ms"] == 125
    assert "cpu_ms" not in completed


def test_stage_error_is_reraised_without_logging_its_message(capsys) -> None:  # type: ignore[no-untyped-def]
    error = ValueError("private-provider-payload")
    with pytest.raises(ValueError) as raised:
        with timed_stage("planning"):
            raise error
    assert raised.value is error
    output = capsys.readouterr().out
    assert "private-provider-payload" not in output
    events = [json.loads(line) for line in output.splitlines()]
    assert [event["outcome"] for event in events] == ["started", "failed"]
    assert events[1]["error_type"] == "ValueError"
