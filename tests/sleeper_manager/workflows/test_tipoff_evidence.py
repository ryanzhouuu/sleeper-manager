"""Lineup reads around a tipoff separate lost games from ones the system cannot verify."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import DueWorkKind
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.lock_in_planning import _merge_planning_evidence
from sleeper_manager.workflows.tipoff_evidence import TipoffLineup, record_tipoff_read

TIP = datetime(2026, 11, 5, 3, tzinfo=UTC)
LAST_READ = TIP - timedelta(minutes=10)


def _row(
    *,
    starting: bool,
    player: str = "acuff",
    game: str = "g1",
    start: datetime = TIP,
    forfeited: bool = False,
) -> LockInOpportunityRecord:
    return LockInOpportunityRecord(
        key=LockInOpportunityKey("league", 3, 6, player, game),
        provider_player_id=f"espn-{player}",
        scheduled_start=start,
        action_deadline=TIP + timedelta(days=2),
        fantasy_week_end=TIP + timedelta(days=5),
        status=Status.SCHEDULED if starting else Status.INELIGIBLE,
        next_check_at=start + timedelta(hours=2),
        created_at=LAST_READ,
        updated_at=LAST_READ,
        slot_index=2 if starting else None,
        slot_position="G" if starting else None,
        eligible_positions=("PG", "SG") if starting else (),
        rostered_at_tipoff=starting,
        roster_evidence_at=LAST_READ,
        league_configuration_fingerprint="league-v1",
        trace_json=json.dumps(
            {"observed_starter": starting, "eligibility_forfeited": forfeited},
        ),
    )


def _lineup(read_at: datetime, *starters: str) -> TipoffLineup:
    return TipoffLineup("league", 3, 6, {player: (2, "G") for player in starters}, read_at=read_at)


def _read(
    tmp_path: Path, rows: tuple[LockInOpportunityRecord, ...], lineup: TipoffLineup
) -> tuple[LockInOpportunityRecord, AsyncSQLiteStateRepository]:
    async def exercise() -> tuple[LockInOpportunityRecord, AsyncSQLiteStateRepository]:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        for row in rows:
            assert await repository.upsert_lock_in_opportunity(row)
        await record_tipoff_read(repository, lineup, tipoff=TIP)
        saved = await repository.get_lock_in_opportunity(rows[0].key)
        assert saved is not None
        return saved, repository

    return asyncio.run(exercise())


def test_before_read_lets_a_late_move_count_as_starting(tmp_path: Path) -> None:
    saved, _ = _read(
        tmp_path, (_row(starting=False),), _lineup(TIP - timedelta(minutes=5), "acuff")
    )
    assert saved.status is Status.SCHEDULED
    assert saved.rostered_at_tipoff is True
    assert saved.slot_position == "G"
    assert saved.tipoff_unverified is False


def test_before_read_marks_a_benched_starter_ineligible(tmp_path: Path) -> None:
    saved, _ = _read(tmp_path, (_row(starting=True),), _lineup(TIP - timedelta(minutes=5)))
    assert saved.status is Status.INELIGIBLE
    assert saved.rostered_at_tipoff is False


def test_before_read_that_lands_after_tipoff_proves_nothing(tmp_path: Path) -> None:
    saved, _ = _read(
        tmp_path, (_row(starting=False),), _lineup(TIP + timedelta(seconds=30), "acuff")
    )
    assert saved.tipoff_unverified is True


def test_benched_then_starting_is_unverified_and_watched(tmp_path: Path) -> None:
    later = _row(starting=True, game="g2", start=TIP + timedelta(days=2))
    saved, repository = _read(
        tmp_path, (_row(starting=False), later), _lineup(TIP + timedelta(minutes=10), "acuff")
    )
    assert saved.status is Status.ACTIVE
    assert saved.rostered_at_tipoff is True
    assert saved.tipoff_unverified is True
    assert saved.slot_index == 2
    assert saved.eligible_positions == ("PG", "SG")
    work = asyncio.run(repository.list_scheduled_work(kind=DueWorkKind.POSTGAME))
    assert [row.game_id for row in work] == ["g1"]


def test_benched_at_both_reads_stays_ineligible(tmp_path: Path) -> None:
    saved, _ = _read(tmp_path, (_row(starting=False),), _lineup(TIP + timedelta(minutes=10)))
    assert saved.status is Status.INELIGIBLE
    assert saved.tipoff_unverified is False


def test_starting_at_both_reads_stays_verified(tmp_path: Path) -> None:
    saved, _ = _read(
        tmp_path, (_row(starting=True),), _lineup(TIP + timedelta(minutes=10), "acuff")
    )
    assert saved.rostered_at_tipoff is True
    assert saved.tipoff_unverified is False


def test_forfeited_game_is_never_reopened(tmp_path: Path) -> None:
    saved, _ = _read(
        tmp_path,
        (_row(starting=False, forfeited=True),),
        _lineup(TIP + timedelta(minutes=10), "acuff"),
    )
    assert saved.status is Status.INELIGIBLE
    assert saved.tipoff_unverified is False


def test_read_after_the_window_does_not_mark_a_mid_game_sub(tmp_path: Path) -> None:
    saved, _ = _read(
        tmp_path, (_row(starting=False),), _lineup(TIP + timedelta(minutes=40), "acuff")
    )
    assert saved.status is Status.INELIGIBLE
    assert saved.tipoff_unverified is False


def test_settled_decisions_never_change(tmp_path: Path) -> None:
    settled = replace(
        _row(starting=False),
        status=Status.ACKNOWLEDGED_PASSED,
        acknowledged_action="passed",
        acknowledged_at=TIP,
    )
    saved, _ = _read(tmp_path, (settled,), _lineup(TIP + timedelta(minutes=10), "acuff"))
    assert saved.status is Status.ACKNOWLEDGED_PASSED
    assert saved.tipoff_unverified is False


def test_later_bench_move_forfeits_an_unverified_game() -> None:
    current = replace(
        _row(starting=False),
        status=Status.ACTIVE,
        rostered_at_tipoff=True,
        tipoff_unverified=True,
    )
    candidate = replace(
        current,
        roster_evidence_at=None,
        rostered_at_tipoff=None,
        trace_json=json.dumps(
            {"observed_starter": False, "observed_at": (TIP + timedelta(hours=1)).isoformat()}
        ),
    )
    assert _merge_planning_evidence(current, candidate).status is Status.INELIGIBLE


def test_new_pre_tipoff_evidence_clears_the_flag_after_a_reschedule() -> None:
    current = replace(_row(starting=True), tipoff_unverified=True)
    candidate = replace(
        current, roster_evidence_at=TIP - timedelta(minutes=1), tipoff_unverified=False
    )
    assert _merge_planning_evidence(current, candidate).tipoff_unverified is False
