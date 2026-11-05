"""Tipoff reads record lineup evidence only, and failed reads stay retryable."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from sleeper_manager.cloudflare.tipoff_dispatch import dispatch_tipoff_reads, tipoff_from_key
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.tipoff_evidence import TipoffLineup

TIP = datetime(2026, 11, 5, 3, tzinfo=UTC)
NOW = TIP + timedelta(minutes=10)


def _work() -> ScheduledWorkRecord:
    return ScheduledWorkRecord(
        work_id="tip",
        dedupe_key=f"tipoff_evidence:after:{TIP.isoformat()}",
        kind=DueWorkKind.TIPOFF_EVIDENCE,
        due_at=NOW,
        status=ScheduledWorkStatus.PENDING,
        deadline=NOW + timedelta(minutes=5),
        created_at=TIP,
        updated_at=TIP,
    )


def _benched_row() -> LockInOpportunityRecord:
    return LockInOpportunityRecord(
        key=LockInOpportunityKey("league", 3, 6, "acuff", "g1"),
        provider_player_id="espn-acuff",
        scheduled_start=TIP,
        action_deadline=TIP + timedelta(days=2),
        fantasy_week_end=TIP + timedelta(days=5),
        status=Status.INELIGIBLE,
        next_check_at=TIP + timedelta(hours=2),
        created_at=TIP - timedelta(days=1),
        updated_at=TIP - timedelta(minutes=5),
        rostered_at_tipoff=False,
        roster_evidence_at=TIP - timedelta(minutes=5),
        trace_json=json.dumps({"eligibility_forfeited": False}),
    )


async def _run(tmp_path: Path, read_lineup: AsyncMock) -> AsyncSQLiteStateRepository:
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.upsert_scheduled_work(_work())
    await repository.upsert_lock_in_opportunity(_benched_row())
    claimed = await repository.claim_due_work(NOW, correlation_id="c")
    await dispatch_tipoff_reads(
        repository,
        claimed,
        read_lineup=read_lineup,
        scheduled_at=NOW,
        correlation_id="c",
        clock=lambda: NOW,
    )
    return repository


def test_key_carries_the_tipoff() -> None:
    assert tipoff_from_key(_work().dedupe_key) == TIP


def test_successful_read_records_evidence_and_completes(tmp_path: Path) -> None:
    async def exercise() -> None:
        lineup = TipoffLineup("league", 3, 6, {"acuff": (2, "G")}, read_at=NOW)
        repository = await _run(tmp_path, AsyncMock(return_value=lineup))
        row = await repository.get_lock_in_opportunity(_benched_row().key)
        assert row is not None and row.tipoff_unverified is True
        work = await repository.list_scheduled_work(kind=DueWorkKind.TIPOFF_EVIDENCE)
        assert work[0].status is ScheduledWorkStatus.COMPLETED

    asyncio.run(exercise())


def test_failed_read_retries_without_changing_eligibility(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = await _run(tmp_path, AsyncMock(side_effect=RuntimeError("sleeper down")))
        row = await repository.get_lock_in_opportunity(_benched_row().key)
        assert row is not None and row.status is Status.INELIGIBLE
        work = await repository.list_scheduled_work(kind=DueWorkKind.TIPOFF_EVIDENCE)
        assert work[0].status is ScheduledWorkStatus.RETRY

    asyncio.run(exercise())


def test_schedule_keeps_two_reads_per_distinct_future_tipoff(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from sleeper_manager.cloudflare import planning_schedule
    from sleeper_manager.domain.nba import GameStatus

    now = TIP - timedelta(hours=1)
    game = SimpleNamespace(start_time=TIP, status=GameStatus.SCHEDULED)
    other = SimpleNamespace(start_time=TIP, status=GameStatus.SCHEDULED)
    started = SimpleNamespace(start_time=now - timedelta(minutes=1), status=GameStatus.SCHEDULED)
    inputs = SimpleNamespace(week_window=SimpleNamespace(contains=lambda _: True))

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        monkeypatch.setattr(planning_schedule, "_roster_games", lambda _: (game, other, started))
        await planning_schedule._replace_future_tipoff_reads(repository, inputs, now)
        work = await repository.list_scheduled_work(kind=DueWorkKind.TIPOFF_EVIDENCE)
        assert sorted(row.due_at for row in work) == [
            TIP - timedelta(minutes=5),
            TIP + timedelta(minutes=10),
        ]

        monkeypatch.setattr(planning_schedule, "_roster_games", lambda _: ())
        await planning_schedule._replace_future_tipoff_reads(repository, inputs, now)
        work = await repository.list_scheduled_work(kind=DueWorkKind.TIPOFF_EVIDENCE)
        assert {row.status for row in work} == {ScheduledWorkStatus.CANCELED}

    asyncio.run(exercise())
