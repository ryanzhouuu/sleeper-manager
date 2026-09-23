"""Guard forecast-only Cron behavior while no advisor runtime policy is active."""

import asyncio
from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import Any

import pytest

from sleeper_manager.cloudflare.runtime import run_scheduled
from sleeper_manager.cloudflare.scheduler_types import ScheduledRunStatus
from sleeper_manager.persistence.d1 import D1_SCHEMA
from sleeper_manager.workflows.forecast_collection import ForecastCapturePolicy
from tests.sleeper_manager.persistence.test_d1 import FakeD1


def test_forecast_capture_runs_without_activating_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing advisor policy blocks planning but still reaches forecast capture."""

    async def exercise() -> None:
        state = FakeD1()
        await state.exec(D1_SCHEMA)
        policies: list[ForecastCapturePolicy] = []

        async def capture(*args: Any, policy: ForecastCapturePolicy, **kwargs: Any) -> None:
            policies.append(policy)

        async def unused_fetch(url: str) -> object:
            raise AssertionError(f"Unexpected provider request: {url}")

        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.capture_scheduled_forecast", capture
        )
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            NTFY_TOPIC="test-topic",
            SLEEPER_LEAGUE_ID="league-1",
            SLEEPER_USER_ID="user-1",
            sleeper_manager_state=state,
            forecast_archive=FakeD1(),
        )
        summary = await run_scheduled(
            env,
            unused_fetch,
            scheduled_at=datetime(2026, 9, 23, 13, tzinfo=UTC),
        )

        assert summary["status"] == ScheduledRunStatus.BLOCKED.value
        assert summary["detail"] == "runtime_policy_missing"
        assert len(policies) == 1
        assert policies[0].manager_timezone == "America/Chicago"
        assert policies[0].daily_plan_time == time(hour=7)

    asyncio.run(exercise())
