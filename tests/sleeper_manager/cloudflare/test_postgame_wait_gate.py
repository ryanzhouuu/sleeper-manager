"""Postgame wakes skip full collection while every claimed final is still unusable."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from sleeper_manager.cloudflare.postgame_dispatch import dispatch_postgame_work
from sleeper_manager.cloudflare.scheduler_types import ScheduledRunStatus
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.base import ScheduledWorkStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord
from tests.sleeper_manager.cloudflare.test_postgame_gate import (
    NOW,
    Reader,
    Repository,
    _row,
    _work,
)
from tests.sleeper_manager.workflows.postgame_lock_in_support import _box, _summary_result

_PLAYING = _row(
    status=LockInOpportunityStatus.ACTIVE,
    stable_score=None,
    current_recommendation_id=None,
    current_observed_at=None,
)


class WaitRepository(Repository):
    def __init__(self, due: tuple[LockInOpportunityRecord, ...], week: tuple[Any, ...] = ()):
        super().__init__(due, week)
        self.deferred: list[LockInOpportunityRecord] = []

    async def update_lock_in_opportunity(
        self, record: LockInOpportunityRecord, *, expected_version: int
    ) -> LockInOpportunityRecord:
        self.deferred.append(record)
        return record


class CollectCalled(BaseException):
    pass


async def _collect(**kwargs: Any) -> Any:
    raise CollectCalled


def _fetch(status: GameStatus = GameStatus.IN_PROGRESS, *, fail: bool = False):  # type: ignore[no-untyped-def]
    result = _summary_result(_box("espn-p1", points=10), status=status, retrieved_at=NOW)

    async def fetch(game_id: str, players: object) -> Any:
        if fail:
            raise OSError("espn down")
        return result

    return fetch


def _dispatch(repository: WaitRepository, fetch, reader: Reader | None = None, work=None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        dispatch_postgame_work(
            repository,  # type: ignore[arg-type]
            work or (_work(),),
            policy=default_runtime_policy(history_version="control"),
            notifications=object(),  # type: ignore[arg-type]
            collect=_collect,
            fetch_summary=fetch,
            scheduled_at=NOW,
            correlation_id="corr",
            open_sleeper_url="https://sleeper.com",
            clock=lambda: NOW + timedelta(seconds=20),
            lock_reader=reader,
        )
    )


def _collected(repository: WaitRepository, fetch, **kwargs: Any) -> bool:  # type: ignore[no-untyped-def]
    try:
        _dispatch(repository, fetch, **kwargs)
    except CollectCalled:
        return True
    return False


def test_game_in_progress_defers_without_collecting() -> None:
    repository = WaitRepository((_PLAYING,))
    attempts = _dispatch(repository, _fetch())
    assert [attempt.outcome for attempt in attempts] == [ScheduledRunStatus.NO_ACTION]
    (finished,) = repository.finished
    assert finished["status"] is ScheduledWorkStatus.RETRY
    assert finished["retry_at"] == NOW + timedelta(minutes=5)
    assert json.loads(finished["terminal_summary_json"])["detail"] == "awaiting_final"
    (deferred,) = repository.deferred
    assert deferred.status is LockInOpportunityStatus.ACTIVE
    assert deferred.next_check_at == NOW + timedelta(minutes=5)
    assert json.loads(deferred.trace_json or "{}") == {"reason_codes": ["game_not_final"]}


def test_usable_final_takes_the_full_pass() -> None:
    repository = WaitRepository((_PLAYING,))
    assert _collected(repository, _fetch(GameStatus.FINAL))
    assert repository.deferred == []


def test_settled_game_beside_a_live_one_skips_when_sleeper_locks_match() -> None:
    settled = _row("p2", "g0")
    repository = WaitRepository((_PLAYING, settled))
    work = (_work(), _work("g0"))
    assert not _collected(repository, _fetch(), reader=Reader(), work=work)
    assert [row.key.game_id for row in repository.deferred] == ["g1"]


def test_settled_game_beside_a_live_one_replans_after_a_new_sleeper_lock() -> None:
    settled = _row("p2", "g0")
    repository = WaitRepository((_PLAYING, settled))
    work = (_work(), _work("g0"))
    assert _collected(repository, _fetch(), reader=Reader({"p2": "sleeper-g0"}), work=work)
    assert repository.deferred == []


def test_missing_due_rows_or_summary_failures_take_the_full_pass() -> None:
    assert _collected(WaitRepository(()), _fetch())
    assert _collected(WaitRepository((_PLAYING,)), _fetch(fail=True))
