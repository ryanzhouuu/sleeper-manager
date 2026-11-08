import asyncio

from sleeper_manager.cloudflare.worker import Default
from tests.sleeper_manager.cloudflare.test_home_route import _Env, _Request


def test_worker_ack_path_is_not_found() -> None:
    entrypoint = Default()
    entrypoint.env = _Env()

    for method in ("GET", "POST"):
        url = "https://worker.test/ack?token=t&action=lock"
        response = asyncio.run(entrypoint.fetch(_Request(method, url)))
        assert response.status_code == 404


def test_scheduled_uses_entrypoint_environment(monkeypatch) -> None:
    seen: list[object] = []

    async def fake_run_scheduled(env, fetcher, **_kwargs):  # type: ignore[no-untyped-def]
        del fetcher
        seen.append(env)
        return {"status": "created"}

    monkeypatch.setattr("sleeper_manager.cloudflare.worker.run_scheduled", fake_run_scheduled)
    entrypoint = Default()
    entrypoint.env = object()

    asyncio.run(entrypoint.scheduled(object(), object(), object()))

    assert seen == [entrypoint.env]
