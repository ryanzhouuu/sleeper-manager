"""Only idempotent schedule writes may repeat after an ambiguous D1 failure."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sleeper_manager.persistence.base import ScheduledWorkStatus
from sleeper_manager.persistence.d1 import D1StateRepository
from tests.sleeper_manager.persistence.test_d1 import D1_SCHEMA, FakeD1, FakeStatement
from tests.sleeper_manager.persistence.test_runtime_persistence import NOW, _work


@pytest.mark.parametrize("committed", [False, True])
def test_schedule_upsert_retries_transient_failure_without_duplicate_work(committed: bool) -> None:
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        original = FakeStatement.run
        calls = 0

        async def fail_once(statement: FakeStatement) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                if committed:
                    await original(statement)
                raise RuntimeError("D1_ERROR: Network connection lost.")
            return await original(statement)

        with patch.object(FakeStatement, "run", fail_once):
            await repository.upsert_scheduled_work(_work())
        assert calls == 2
        assert await repository.list_scheduled_work() == (_work(),)

    asyncio.run(exercise())


def test_schedule_upsert_stops_retrying_persistent_connection_failure() -> None:
    async def exercise() -> None:
        statement = AsyncMock(side_effect=RuntimeError("D1_ERROR: Network connection lost."))
        repository = D1StateRepository(FakeD1())
        with (
            patch.object(repository, "_run", statement),
            patch(
                "sleeper_manager.persistence.d1_retry.asyncio.sleep", new_callable=AsyncMock
            ) as wait,
        ):
            with pytest.raises(RuntimeError, match="Network connection lost"):
                await repository.upsert_scheduled_work(_work())
        assert statement.await_count == 3
        assert wait.await_count == 2
        assert 0.1 <= wait.await_args_list[0].args[0] <= 0.2
        assert 0.2 <= wait.await_args_list[1].args[0] <= 0.4

    asyncio.run(exercise())


def test_schedule_upsert_does_not_retry_permanent_errors() -> None:
    async def exercise() -> None:
        statement = AsyncMock(side_effect=RuntimeError("D1_ERROR: no such table: scheduled_work"))
        repository = D1StateRepository(FakeD1())
        with patch.object(repository, "_run", statement):
            with pytest.raises(RuntimeError, match="no such table"):
                await repository.upsert_scheduled_work(_work())
        assert statement.await_count == 1

    asyncio.run(exercise())


def test_work_claim_does_not_retry_an_ambiguous_committed_write() -> None:
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await repository.upsert_scheduled_work(_work())
        original = FakeStatement.all
        calls = 0

        async def lose_response(statement: FakeStatement) -> dict[str, object]:
            nonlocal calls
            calls += 1
            await original(statement)
            raise RuntimeError("D1_ERROR: Network connection lost.")

        with patch.object(FakeStatement, "all", lose_response):
            with pytest.raises(RuntimeError, match="Network connection lost"):
                await repository.claim_due_work(NOW, correlation_id="run-1")
        assert calls == 1
        work = (await repository.list_scheduled_work())[0]
        assert work.status is ScheduledWorkStatus.RUNNING
        assert work.attempt_count == 1

    asyncio.run(exercise())
