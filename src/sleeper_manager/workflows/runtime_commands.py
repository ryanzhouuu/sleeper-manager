"""Local scheduled execution and explicit Cloudflare data synchronization commands."""

import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from sleeper_manager.cloudflare.dispatcher import dispatch_due_work
from sleeper_manager.cloudflare.planning import collect_cloudflare_planning_inputs
from sleeper_manager.cloudflare.providers import CloudflareESPNProvider, CloudflareSleeperClient
from sleeper_manager.cloudflare.runtime_sync import (
    RemoteD1,
    compact_history_from_workspace,
    d1_database_id,
    redact_secrets,
    runtime_policy_for_history,
    sync_cloudflare_runtime_data,
)
from sleeper_manager.config import Settings
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.integrations.nba.cached_provider import AsyncCachedNBAProvider
from sleeper_manager.notifications.factory import build_notification_dispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.forecast_sqlite import AsyncSQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_collection import capture_scheduled_forecast
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
)
from sleeper_manager.workflows.postgame_lock_in import LOCK_IN_ACKNOWLEDGEMENT_KINDS


class _HttpxFetchResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self._payload = response.json()

    def json(self) -> object:
        return self._payload


async def _run_scheduled(settings: Settings) -> int:
    if not settings.sleeper_configured:
        print(
            "Sleeper configuration is incomplete: set SLEEPER_LEAGUE_ID and SLEEPER_USER_ID",
            file=sys.stderr,
        )
        return 2
    if not settings.notifications_configured:
        print("Notification configuration is incomplete", file=sys.stderr)
        return 2
    if not settings.acknowledgement_base_url:
        print("Set ACKNOWLEDGEMENT_BASE_URL before running scheduled work", file=sys.stderr)
        return 2
    try:
        repository = AsyncSQLiteStateRepository(settings.sqlite_path)
        await repository.initialize()
        notifications = NotificationLoop(
            repository,
            build_notification_dispatcher(settings),
            acknowledgement_base_url=settings.acknowledgement_base_url,
            acknowledgement_kinds=LOCK_IN_ACKNOWLEDGEMENT_KINDS,
        )
        now = datetime.now(UTC)
        env = SimpleNamespace(
            SLEEPER_LEAGUE_ID=settings.sleeper_league_id,
            SLEEPER_USER_ID=settings.sleeper_user_id,
        )
        async with httpx.AsyncClient() as client:

            async def fetch(url: str) -> _HttpxFetchResponse:
                return _HttpxFetchResponse(await client.get(url))

            async def collect(*, repository, policy, scheduled_at):  # type: ignore[no-untyped-def]
                return await collect_cloudflare_planning_inputs(
                    env,
                    fetch,
                    repository=repository,
                    policy=policy,
                    scheduled_at=scheduled_at,
                    clock=lambda: now,
                )

            summary = await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=now,
                correlation_id=uuid4().hex,
                open_sleeper_url="https://sleeper.com",
                fetch_game_summary=CloudflareESPNProvider(fetch, clock=lambda: now).game_summary,
            )

            async def fetch_raw(url: str) -> httpx.Response:
                return await client.get(url)

            await _capture_local_forecast(
                settings,
                repository,
                CloudflareSleeperClient(fetch),
                nba=AsyncCachedNBAProvider(
                    CloudflareESPNProvider(fetch, clock=lambda: now),
                    repository,
                    clock=lambda: now,
                ),
                fetch=fetch_raw,
                now=now,
            )
    except (OSError, RuntimeError, ValueError) as error:
        print(redact_secrets(f"Scheduled run failed: {error}"), file=sys.stderr)
        return 2
    print(json.dumps(summary.as_dict(), sort_keys=True))
    if summary.status.value in {"failed", "blocked"}:
        return 2
    return 0 if summary.status.value != "delivery_failed" else 1


async def _capture_local_forecast(
    settings: Settings,
    repository: AsyncSQLiteStateRepository,
    sleeper: CloudflareSleeperClient,
    *,
    nba: AsyncCachedNBAProvider,
    fetch: Callable[[str], Awaitable[object]],
    now: datetime,
) -> None:
    """Write the local forecast archive without changing the planning exit status."""

    try:
        record = await repository.load_runtime_policy()
        if record is None or not settings.sleeper_league_id or not settings.sleeper_user_id:
            return
        archive = AsyncSQLiteForecastArchiveRepository(
            settings.sqlite_path.with_name("forecasts.db")
        )
        await archive.initialize()
        await capture_scheduled_forecast(
            archive,
            repository,
            sleeper,
            policy=RuntimePolicy.from_json(record.version, record.payload_json),
            nba=nba,
            fetch=fetch,
            now=now,
            league_id=settings.sleeper_league_id,
            user_id=settings.sleeper_user_id,
        )
    except Exception:
        print("Forecast capture failed", file=sys.stderr)


async def _sync_cloudflare_runtime_data(
    *,
    workspace: Path,
    league_fixture: Path,
    apply: bool,
    wrangler_config: Path,
    settings: Settings,
) -> int:
    now = datetime.now(UTC)
    try:
        history = compact_history_from_workspace(
            workspace,
            league_fixture=league_fixture,
            now=now,
        )
        policy = runtime_policy_for_history(history, policy_path=settings.manager_policy_path)
        repository = None
        if apply:
            account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
            api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
            if not account_id or not api_token:
                print(
                    "Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN before --apply",
                    file=sys.stderr,
                )
                return 2
            async with httpx.AsyncClient() as client:
                repository = D1StateRepository(
                    RemoteD1(
                        account_id,
                        d1_database_id(wrangler_config),
                        api_token,
                        client,
                    )
                )
                report = await sync_cloudflare_runtime_data(
                    history,
                    policy=policy,
                    repository=repository,
                    apply=True,
                    now=now,
                )
        else:
            report = await sync_cloudflare_runtime_data(
                history,
                policy=policy,
                repository=None,
                apply=False,
                now=now,
            )
    except (OSError, RuntimeError, ValueError) as error:
        print(redact_secrets(f"Runtime data sync failed: {error}"), file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0
