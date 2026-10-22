"""Daily confirmations use local dates, retain warnings, and expire with their evidence."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sleeper_manager.workflows.daily_completion import notify_daily_completion
from sleeper_manager.workflows.daily_plan import run_daily_plan
from tests.sleeper_manager.persistence.test_d1 import recommendation
from tests.sleeper_manager.workflows.test_daily_plan import (
    NOW,
    WINDOW_END,
    _inputs,
    _projections,
    _workflow,
)
from tests.sleeper_manager.workflows.test_injury_fetch_failure import _failed_injuries


def test_confirmation_renews_at_local_midnight_instead_of_utc_midnight(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        observed = [datetime(2026, 1, 7, 23, 59, tzinfo=UTC)]
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: observed[0])
        await repository.initialize()
        record = replace(recommendation(), deadline=WINDOW_END, trace_json="{}")
        identities = []
        for at, expected_status in (
            (datetime(2026, 1, 7, 23, 59, tzinfo=UTC), "created"),
            (datetime(2026, 1, 8, 0, 1, tzinfo=UTC), "duplicate"),
            (datetime(2026, 1, 8, 6, 0, tzinfo=UTC), "created"),
        ):
            observed[0] = at
            result = await notify_daily_completion(
                record,
                notifications,
                now=at,
                local_timezone=ZoneInfo("America/Chicago"),
                open_sleeper_url="https://sleeper.com",
            )
            assert result.status == expected_status
            assert result.recommendation.deadline > at
            identities.append(result.recommendation.recommendation_id)
        assert identities[0] == identities[1]
        assert identities[2] != identities[0]
        assert len(sender.messages) == 2
        assert sender.messages[0].actions == sender.messages[1].actions == ()

    asyncio.run(exercise())


def test_confirmation_cannot_extend_expired_weekly_evidence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        result = await notify_daily_completion(
            replace(recommendation(), deadline=NOW),
            notifications,
            now=NOW,
            local_timezone=ZoneInfo("America/Chicago"),
            open_sleeper_url="https://sleeper.com",
        )
        assert result is None
        assert sender.messages == []

    asyncio.run(exercise())


def test_daily_no_change_confirmation_preserves_provisional_injury_warning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        result = await run_daily_plan(
            _failed_injuries(_inputs(projections=_projections())),
            decision_time=NOW,
            repository=repository,
            notifications=notifications,
            open_sleeper_url="https://sleeper.com",
            clock=lambda: NOW,
            daily_confirmation=True,
        )
        assert result.outcome == "notified"
        assert result.plan.status.value == "degraded"
        assert "injury updates are unavailable" in sender.messages[0].message.lower()
        assert "provisional" in sender.messages[0].message.lower()
        assert sender.messages[0].actions == ()

    asyncio.run(exercise())


def test_confirmation_deadline_is_local_midnight_across_dst(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        at = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        repository, _, notifications = _workflow(tmp_path, clock=lambda: at)
        await repository.initialize()
        result = await notify_daily_completion(
            replace(recommendation(), deadline=at + timedelta(days=2)),
            notifications,
            now=at,
            local_timezone=ZoneInfo("America/Chicago"),
            open_sleeper_url="https://sleeper.com",
        )
        assert result.recommendation.deadline == datetime(2026, 11, 2, 6, tzinfo=UTC)

    asyncio.run(exercise())
