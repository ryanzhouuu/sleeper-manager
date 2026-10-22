"""Serve public reads, scoped confirmations, owner controls, Cron, and planning queues.

Cron captures forecasts and publishes wakes; queue consumers execute durable due work.
Bindings and secrets come from the Worker environment.
"""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from sleeper_manager.cloudflare.confirmation import confirmation
from sleeper_manager.cloudflare.home import build_home
from sleeper_manager.cloudflare.manager_routes import manage
from sleeper_manager.cloudflare.providers import CloudflareESPNProvider, CloudflareSleeperClient
from sleeper_manager.cloudflare.runtime import run_queued_planning, run_scheduled
from sleeper_manager.persistence.manager_store import D1ManagerStore

try:
    from workers import Response, fetch  # type: ignore[import-not-found]
    from workers import WorkerEntrypoint as _WorkerEntrypoint
except ImportError:  # pragma: no cover - the Workers SDK exists only in the deploy runtime
    fetch = None

    class _FallbackResponse:
        """Local stand-in for Workers `Response` so route tests can read status and JSON."""

        def __init__(
            self, body: object = None, status: int = 200, headers: dict[str, str] | None = None
        ) -> None:
            self.body = body
            self.headers = headers or {}
            self.status_code = status
            self.payload: object | None = None

        @staticmethod
        def json(payload: object, status: int = 200) -> "_FallbackResponse":
            response = _FallbackResponse(status=status)
            response.payload = payload
            return response

    class _WorkerEntrypoint:  # type: ignore[no-redef]
        pass

    Response = _FallbackResponse


def _query_values(url: str) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in parse_qs(urlparse(url).query, keep_blank_values=False).items()
        if values
    }


def _env_value(env: object, name: str, default: str = "") -> str:
    """Read one Worker binding. Missing and explicit None both use the default."""
    value = getattr(env, name, default)
    return str(value) if value is not None else default


class Default(_WorkerEntrypoint):  # type: ignore[misc]
    """Workers SDK entry: home and confirmation HTTP plus the five-minute Cron."""

    async def fetch(self, request):  # type: ignore[no-untyped-def]
        parsed = urlparse(request.url)
        if parsed.path == "/health":
            return Response.json({"status": "ok"})
        if parsed.path.startswith("/manage"):
            form = await manage(request, self.env, fetch, now=datetime.now(UTC))
            return Response(form.body, status=form.status, headers=form.headers)
        if parsed.path == "/home":
            if request.method != "GET":
                return Response.json({"status": "method_not_allowed"}, status=405)
            league_id = _env_value(self.env, "SLEEPER_LEAGUE_ID")
            user_id = _env_value(self.env, "SLEEPER_USER_ID")
            if not league_id or not user_id:
                return Response.json({"status": "league_unavailable"}, status=503)
            repository = D1ManagerStore(self.env.sleeper_manager_state)
            await repository.initialize()
            result = await build_home(
                sleeper=CloudflareSleeperClient(fetch),
                espn=CloudflareESPNProvider(fetch),
                repository=repository,
                league_id=league_id,
                user_id=user_id,
                timezone_name=_env_value(self.env, "TIMEZONE", "America/Chicago"),
                now=datetime.now(UTC),
            )
            return Response.json(result.payload, status=result.status_code)
        if parsed.path != "/ack":
            return Response("Not found", status=404)

        form = await confirmation(
            request,
            D1ManagerStore(self.env.sleeper_manager_state),
            _query_values(request.url),
            now=datetime.now(UTC),
            env=self.env,
        )
        return Response(form.body, status=form.status, headers=form.headers)

    async def scheduled(self, controller, env, ctx):  # type: ignore[no-untyped-def]
        del env, ctx
        result = await run_scheduled(self.env, fetch, controller=controller)
        print(json.dumps(result, sort_keys=True))

    async def queue(self, batch, env=None, ctx=None):  # type: ignore[no-untyped-def]
        """Acknowledge each signal only after its durable work attempt returns."""
        del env, ctx
        for message in batch.messages:
            result = await run_queued_planning(self.env, fetch, message.body)
            print(json.dumps(result, sort_keys=True))
            message.ack()
