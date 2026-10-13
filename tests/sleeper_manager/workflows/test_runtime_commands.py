"""Local scheduled execution shares the Worker body and archive contracts."""

import asyncio
from types import SimpleNamespace

import httpx

from sleeper_manager.config import Settings
from sleeper_manager.workflows import runtime_commands as module


def test_binary_response_does_not_attempt_json_decode() -> None:
    response = module._HttpxFetchResponse(
        httpx.Response(200, content=b"%PDF-source", headers={"content-length": "11"})
    )
    assert response.status == 200
    assert response.headers.get("content-length") == "11"
    assert asyncio.run(response.bytes()) == b"%PDF-source"
    assert module._HttpxFetchResponse(httpx.Response(200, json={"value": 1})).json() == {"value": 1}


def test_local_schedule_passes_archives_and_completed_collection_clock(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    events = []

    async def capture(*args, **kwargs):  # type: ignore[no-untyped-def]
        events.append("capture")

    async def collect(env, fetch, **kwargs):  # type: ignore[no-untyped-def]
        events.append("collect")
        assert "clock" not in kwargs
        assert kwargs["shadow_repository"] is not None
        assert kwargs["forecast_archive"] is not None

    async def dispatch(repository, **kwargs):  # type: ignore[no-untyped-def]
        await kwargs["collect"](
            repository=repository, policy=None, scheduled_at=kwargs["scheduled_at"]
        )
        return SimpleNamespace(
            status=SimpleNamespace(value="complete"), as_dict=lambda: {"status": "complete"}
        )

    monkeypatch.setattr(module, "_capture_local_forecast", capture)
    monkeypatch.setattr(module, "collect_cloudflare_planning_inputs", collect)
    monkeypatch.setattr(module, "dispatch_due_work", dispatch)
    monkeypatch.setattr(module, "build_notification_dispatcher", lambda settings: object())
    settings = Settings(
        _env_file=None,
        sleeper_league_id="fixture",
        sleeper_user_id="fixture",
        ntfy_topic="fixture",
        acknowledgement_base_url="https://fixture.example/ack",
        sqlite_path=tmp_path / "state.db",
    )
    assert asyncio.run(module._run_scheduled(settings)) == 0
    assert events == ["capture", "collect"]
