import asyncio
from datetime import UTC, datetime

from sleeper_manager.cloudflare.routes import RouteResponse
from sleeper_manager.cloudflare.worker import Default


class _Env:
    SLEEPER_LEAGUE_ID = "league-1"
    SLEEPER_USER_ID = "user-1"
    TIMEZONE = "America/Chicago"
    sleeper_manager_state = object()


class _Request:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url


def test_get_home_uses_worker_configuration(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    async def fake_build_home(**kwargs: object) -> RouteResponse:
        seen.update(kwargs)
        return RouteResponse(
            200,
            {"league": {"name": "Lock In", "week": 3, "timezone": "America/Chicago"}},
        )

    async def fake_initialize(self) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("sleeper_manager.cloudflare.worker.build_home", fake_build_home)
    monkeypatch.setattr(
        "sleeper_manager.cloudflare.worker.D1ManagerStore.initialize",
        fake_initialize,
    )
    entrypoint = Default()
    entrypoint.env = _Env()

    response = asyncio.run(entrypoint.fetch(_Request("GET", "https://worker.test/home")))

    assert response.status_code == 200
    assert seen["league_id"] == "league-1"
    assert seen["user_id"] == "user-1"
    assert seen["timezone_name"] == "America/Chicago"
    assert isinstance(seen["now"], datetime)
    assert seen["now"].tzinfo is UTC


def test_post_home_is_method_not_allowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fail_build_home(**kwargs: object) -> RouteResponse:
        raise AssertionError(kwargs)

    monkeypatch.setattr("sleeper_manager.cloudflare.worker.build_home", fail_build_home)
    entrypoint = Default()
    entrypoint.env = _Env()

    response = asyncio.run(
        entrypoint.fetch(_Request("POST", "https://worker.test/home?token=secret"))
    )

    assert response.status_code == 405
    assert response.payload == {"status": "method_not_allowed"}


def test_missing_league_configuration_is_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fail_build_home(**kwargs: object) -> RouteResponse:
        raise AssertionError(kwargs)

    monkeypatch.setattr("sleeper_manager.cloudflare.worker.build_home", fail_build_home)
    entrypoint = Default()
    entrypoint.env = _Env()
    entrypoint.env.SLEEPER_LEAGUE_ID = ""

    response = asyncio.run(entrypoint.fetch(_Request("GET", "https://worker.test/home")))

    assert response.status_code == 503
    assert response.payload == {"status": "league_unavailable"}


def test_unknown_path_stays_not_found() -> None:
    entrypoint = Default()
    entrypoint.env = _Env()
    response = asyncio.run(entrypoint.fetch(_Request("GET", "https://worker.test/missing")))
    assert response.status_code == 404
