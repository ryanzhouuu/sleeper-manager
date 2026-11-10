"""Settled postgame watches skip full collection only while nothing can have changed."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sleeper_manager.cloudflare.postgame_dispatch import dispatch_postgame_work
from sleeper_manager.cloudflare.postgame_gate import unchanged_postgame_detail
from sleeper_manager.cloudflare.scheduler_types import ScheduledRunStatus
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.base import (
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)

NOW = datetime(2026, 11, 5, 4, 0, tzinfo=UTC)


def _row(player_id: str = "p1", game_id: str = "g1", **changes: Any) -> LockInOpportunityRecord:
    row = LockInOpportunityRecord(
        key=LockInOpportunityKey("league", 3, 1, player_id, game_id),
        provider_player_id=f"espn-{player_id}",
        scheduled_start=NOW - timedelta(hours=3),
        action_deadline=NOW + timedelta(days=2),
        fantasy_week_end=NOW + timedelta(days=4),
        status=LockInOpportunityStatus.ACTIONABLE,
        next_check_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(minutes=5),
        stable_score=31.5,
        current_recommendation_id="rec-1",
        current_observed_at=NOW - timedelta(minutes=5),
    )
    return replace(row, **changes)


def _work(game_id: str | None = "g1") -> ScheduledWorkRecord:
    return ScheduledWorkRecord(
        work_id=f"work-{game_id}",
        dedupe_key=f"postgame:{game_id}",
        kind=DueWorkKind.POSTGAME,
        due_at=NOW - timedelta(minutes=5),
        status=ScheduledWorkStatus.RUNNING,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=5),
        game_id=game_id,
        attempt_count=40,
    )


class Repository:
    def __init__(self, due: tuple[LockInOpportunityRecord, ...], week: tuple[Any, ...] = ()):
        self.due = due
        self.week = week or due
        self.finished: list[dict[str, Any]] = []

    async def list_due_lock_in_opportunities(self, now: datetime) -> tuple[Any, ...]:
        return self.due

    async def list_week_lock_in_opportunities(self, league_id: str, week: int) -> tuple[Any, ...]:
        return self.week

    async def finish_scheduled_work(self, work_id: str, **kwargs: Any) -> None:
        self.finished.append({"work_id": work_id, **kwargs})


class Reader:
    def __init__(self, starters_games: dict[str, str] | None = None, fail: bool = False):
        self.payload = [{"roster_id": 1, "starters": ["p1"], "starters_games": starters_games}]
        self.fail = fail
        self.calls = 0

    async def matchup_legs(self, league_id: str, week: int) -> object:
        self.calls += 1
        if self.fail:
            raise OSError("sleeper down")
        return self.payload

    async def player_game_stats(self, player_id: str, season: int) -> dict[str, Any]:
        raise AssertionError("the settled check never reads per-game stats")


def _check(
    repository: Repository,
    reader: Reader | None,
    work: tuple[ScheduledWorkRecord, ...] | None = None,
) -> bool:
    async def fetch_summary(game_id, players):  # type: ignore[no-untyped-def]
        raise OSError("unsettled games read the final")

    detail = asyncio.run(
        unchanged_postgame_detail(
            work or (_work(),),
            repository=repository,  # type: ignore[arg-type]
            lock_reader=reader,
            fetch_summary=fetch_summary,
            now=NOW,
            recheck_from=NOW,
        )
    )
    return detail == "settled_unchanged"


def test_settled_games_with_unchanged_sleeper_locks_skip_collection() -> None:
    reader = Reader()
    assert _check(Repository((_row(),)), reader)
    assert reader.calls == 1


def test_matching_sleeper_synced_lock_still_counts_as_unchanged() -> None:
    locked = _row(
        "p2",
        "g0",
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        trace_json=json.dumps({"lock_source": "sleeper"}),
    )
    repository = Repository((_row(),), week=(_row(), locked))
    assert _check(repository, Reader({"p2": "sleeper-g0"}))


@pytest.mark.parametrize(
    "row",
    [
        _row(status=LockInOpportunityStatus.FINALIZING),
        _row(stable_score=None),
        _row(current_recommendation_id=None),
    ],
    ids=["stabilizing", "no_stable_score", "no_advice"],
)
def test_unsettled_or_stale_games_take_the_full_pass(row: LockInOpportunityRecord) -> None:
    reader = Reader()
    assert not _check(Repository((row,)), reader)
    assert reader.calls == 0


def test_long_settled_games_stay_skipped_without_a_periodic_full_pass() -> None:
    row = _row(current_observed_at=NOW - timedelta(days=1))
    assert _check(Repository((row,)), Reader())


def test_new_sleeper_lock_takes_the_full_pass() -> None:
    assert not _check(Repository((_row(),)), Reader({"p1": "sleeper-g1"}))


@pytest.mark.parametrize(
    ("reader", "work"),
    [
        (None, (_work(),)),
        (Reader(fail=True), (_work(),)),
        (Reader(), (_work(None),)),
        (Reader(), (_work(), _work("g2"))),
    ],
    ids=["no_reader", "sleeper_unavailable", "missing_game", "game_without_due_rows"],
)
def test_missing_evidence_takes_the_full_pass(reader: Reader | None, work) -> None:  # type: ignore[no-untyped-def]
    assert not _check(Repository((_row(),)), reader, work)


def test_dispatch_finishes_settled_work_without_collecting() -> None:
    repository = Repository((_row(),))

    async def collect(**kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("settled unchanged work must not collect planning inputs")

    async def fetch_summary(game_id, players):  # type: ignore[no-untyped-def]
        raise AssertionError("settled unchanged work must not refetch the final")

    attempts = asyncio.run(
        dispatch_postgame_work(
            repository,  # type: ignore[arg-type]
            (_work(),),
            policy=default_runtime_policy(history_version="control"),
            notifications=object(),  # type: ignore[arg-type]
            collect=collect,
            fetch_summary=fetch_summary,
            scheduled_at=NOW,
            correlation_id="corr",
            open_sleeper_url="https://sleeper.com",
            clock=lambda: NOW,
            lock_reader=Reader(),
        )
    )
    assert [attempt.outcome for attempt in attempts] == [ScheduledRunStatus.NO_ACTION]
    (finished,) = repository.finished
    assert finished["status"] is ScheduledWorkStatus.RETRY
    assert finished["retry_at"] == NOW + timedelta(minutes=5)
    assert json.loads(finished["terminal_summary_json"])["detail"] == "settled_unchanged"
