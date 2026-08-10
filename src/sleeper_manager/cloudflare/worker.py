import json
from urllib.parse import parse_qs, urlparse

from sleeper_manager.cloudflare.routes import acknowledge
from sleeper_manager.cloudflare.runtime import run_scheduled
from sleeper_manager.persistence.d1 import D1StateRepository

try:
    from workers import Response, fetch  # type: ignore[import-not-found]
    from workers import WorkerEntrypoint as _WorkerEntrypoint
except ImportError:  # pragma: no cover - the Workers SDK exists only in the deploy runtime
    Response = object
    fetch = None

    class _WorkerEntrypoint:  # type: ignore[no-redef]
        pass


def _query_values(url: str) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in parse_qs(urlparse(url).query, keep_blank_values=False).items()
        if values
    }


class Default(_WorkerEntrypoint):  # type: ignore[misc]
    async def fetch(self, request):  # type: ignore[no-untyped-def]
        parsed = urlparse(request.url)
        if parsed.path == "/health":
            return Response.json({"status": "ok"})
        if parsed.path != "/ack":
            return Response("Not found", status=404)

        repository = D1StateRepository(self.env.sleeper_manager_state)
        await repository.initialize()
        result = await acknowledge(repository, _query_values(request.url))
        return Response.json(result.payload, status=result.status_code)

    async def scheduled(self, controller, env, ctx):  # type: ignore[no-untyped-def]
        del controller, env, ctx
        result = await run_scheduled(self.env, fetch)
        print(json.dumps(result, sort_keys=True))
