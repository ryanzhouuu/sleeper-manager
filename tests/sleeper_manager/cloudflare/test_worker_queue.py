"""Queue acknowledgement follows successful processing and preserves failed messages."""

import asyncio
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.worker import Default


@pytest.mark.parametrize("fails", [False, True])
def test_queue_acknowledges_only_after_handler_returns(monkeypatch, fails) -> None:  # type: ignore[no-untyped-def]
    events = []
    environment = object()

    async def process(env, fetcher, body):  # type: ignore[no-untyped-def]
        assert env is environment
        assert body == "wake body"
        events.append("process")
        if fails:
            raise OSError("transport retry required")
        return {"status": "no_action"}

    monkeypatch.setattr("sleeper_manager.cloudflare.worker.run_queued_planning", process)
    entrypoint = Default()
    entrypoint.env = environment
    batch = SimpleNamespace(
        messages=[SimpleNamespace(body="wake body", ack=lambda: events.append("ack"))]
    )
    if fails:
        with pytest.raises(OSError):
            asyncio.run(entrypoint.queue(batch, object(), object()))
        assert events == ["process"]
    else:
        asyncio.run(entrypoint.queue(batch, object(), object()))
        assert events == ["process", "ack"]
