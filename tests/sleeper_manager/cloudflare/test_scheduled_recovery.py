"""Late-stage failures release owned claims and retain durable delivery deduplication."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from sleeper_manager.cloudflare.dispatcher import dispatch_due_work
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkRecord, ScheduledWorkStatus
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import (
    NOW,
    PLAN_POLICY,
    _assembly,
    _collector,
    _dispatch,
    _save_policy,
)
from tests.sleeper_manager.workflows.test_daily_plan import _workflow


@pytest.mark.parametrize(
    "boundary",
    [
        "_replace_future_pre_tipoff",
        "sync_live_lock_in_opportunities",
        "refresh_terminal_lock_in_scores",
        "run_daily_plan",
    ],
)
def test_failure_after_collection_retries_all_unfinished_claims(tmp_path, boundary) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        for kind in (DueWorkKind.POSTGAME, DueWorkKind.DELIVERY_RETRY):
            await repository.upsert_scheduled_work(
                ScheduledWorkRecord(
                    work_id=kind.value,
                    dedupe_key=kind.value,
                    kind=kind,
                    due_at=NOW,
                    status=ScheduledWorkStatus.PENDING,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        failed_at = NOW + timedelta(minutes=2)
        collect, _ = _collector(_assembly())
        failure = RuntimeError("D1_ERROR: Network connection lost. private-provider-payload")
        with patch(
            f"sleeper_manager.cloudflare.dispatcher.{boundary}", AsyncMock(side_effect=failure)
        ):
            result = await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=NOW,
                correlation_id="failed-run",
                open_sleeper_url="https://sleeper.com",
                clock=lambda: failed_at,
                fetch_game_summary=AsyncMock(),
                plan_policy=PLAN_POLICY,
            )
        assert result.status.value == "failed"
        assert result.failure_category.value == "persistence"
        assert len(result.attempts) == 3
        for work in await repository.list_scheduled_work():
            if work.attempt_count == 0:
                continue
            assert work.status is ScheduledWorkStatus.RETRY
            assert work.lease_expires_at is None
            assert work.updated_at == failed_at
            assert work.due_at == failed_at + timedelta(minutes=5)
            assert "private-provider-payload" not in work.terminal_summary_json
            assert json.loads(work.terminal_summary_json)["error_type"] == "RuntimeError"
        assert sender.messages == []
        retry, calls = await _dispatch(repository, notifications, scheduled_at=failed_at)
        assert retry.claimed_count == 0
        assert calls == []

    asyncio.run(exercise())


def test_recovery_write_failure_propagates_for_queue_redelivery(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, _, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        with (
            patch(
                "sleeper_manager.cloudflare.dispatcher._replace_future_pre_tipoff",
                AsyncMock(side_effect=RuntimeError("D1_ERROR: Network connection lost.")),
            ),
            patch.object(
                repository, "finish_scheduled_work", AsyncMock(side_effect=OSError("private-url"))
            ),
        ):
            with pytest.raises(OSError, match="private-url"):
                await _dispatch(repository, notifications, scheduled_at=NOW)
        daily = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert daily.status is ScheduledWorkStatus.RUNNING

    asyncio.run(exercise())
    output = capsys.readouterr().out
    assert "scheduled_recovery_failed" in output
    assert "private-url" not in output


def test_timeout_cancellation_releases_work_and_propagates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, _, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        with patch(
            "sleeper_manager.cloudflare.dispatcher._replace_future_pre_tipoff",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _dispatch(repository, notifications, scheduled_at=NOW)
        daily = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert daily.status is ScheduledWorkStatus.RETRY
        assert daily.lease_expires_at is None

    asyncio.run(exercise())


def test_failure_recording_completion_retries_without_duplicate_delivery(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        finish = repository.finish_scheduled_work
        count = 0

        async def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("D1_ERROR: Network connection lost.")
            return await finish(*args, **kwargs)

        with patch.object(repository, "finish_scheduled_work", fail_once):
            first, _ = await _dispatch(repository, notifications, scheduled_at=NOW)
        assert first.status.value == "failed"
        assert len(sender.messages) == 1
        # The second attempt uses the same evidence to exercise durable delivery identity.
        second, _ = await _dispatch(
            repository, notifications, scheduled_at=NOW + timedelta(minutes=5)
        )
        assert second.status.value == "duplicate"
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_recovery_never_releases_a_new_owners_lease(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, _, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)

        async def reclaim(*args, **kwargs):  # type: ignore[no-untyped-def]
            await repository.claim_due_work(NOW + timedelta(minutes=15), correlation_id="new-owner")
            raise RuntimeError("D1_ERROR: Network connection lost.")

        with patch("sleeper_manager.cloudflare.dispatcher._replace_future_pre_tipoff", reclaim):
            await _dispatch(repository, notifications, scheduled_at=NOW)
        daily = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert daily.status is ScheduledWorkStatus.RUNNING
        assert daily.correlation_id == "new-owner"
        assert daily.attempt_count == 2

    asyncio.run(exercise())


def test_completed_daily_work_survives_a_later_delivery_retry_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        await repository.upsert_scheduled_work(
            ScheduledWorkRecord(
                work_id="delivery-retry",
                dedupe_key="delivery-retry",
                kind=DueWorkKind.DELIVERY_RETRY,
                due_at=NOW,
                status=ScheduledWorkStatus.PENDING,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        with patch(
            "sleeper_manager.cloudflare.dispatcher._run_delivery_retry",
            AsyncMock(side_effect=RuntimeError("unexpected failure")),
        ):
            result, _ = await _dispatch(repository, notifications, scheduled_at=NOW)
        assert result.status.value == "failed"
        assert result.failure_category.value == "internal_invariant"
        daily = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert daily.status is ScheduledWorkStatus.COMPLETED
        retry = (await repository.list_scheduled_work(kind=DueWorkKind.DELIVERY_RETRY))[0]
        assert retry.status is ScheduledWorkStatus.RETRY
        assert len(sender.messages) == 1

    asyncio.run(exercise())
