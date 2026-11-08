"""Reconcile live opportunity rows with the locks Sleeper reports."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.integrations.sleeper.matchup_legs import MatchupLeg
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.sleeper_locks import (
    LOCK_SOURCE_SLEEPER,
    reconcile_sleeper_locks,
)

NOW = datetime(2026, 11, 7, 18, tzinfo=UTC)
TIPOFF = datetime(2026, 11, 6, 0, 30, tzinfo=UTC)
GAME_DATE = date(2026, 11, 5)
POSITIONS = ("PG", "SG", "SF", "PF", "C", "G", "F", "UTIL", "UTIL")
STARTERS = ("p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8")


def record(
    player_id: str = "p2",
    game_id: str = "espn-1",
    *,
    status: LockInOpportunityStatus = LockInOpportunityStatus.ACTIONABLE,
    start: datetime = TIPOFF,
    deadline: datetime = NOW + timedelta(hours=6),
    **changes: object,
) -> LockInOpportunityRecord:
    base = LockInOpportunityRecord(
        key=LockInOpportunityKey("league-1", 3, 6, player_id, game_id),
        provider_player_id="401",
        scheduled_start=start,
        action_deadline=deadline,
        fantasy_week_end=NOW + timedelta(days=2),
        status=status,
        next_check_at=NOW,
        created_at=TIPOFF,
        updated_at=TIPOFF,
        slot_index=1,
        slot_position="SG",
        eligible_positions=("SF", "PF"),
        rostered_at_tipoff=True,
        stable_score=41.5,
        trace_json=json.dumps({"observed_starter": True}),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def leg(starters_games: dict[str, str] | None = None) -> MatchupLeg:
    return MatchupLeg(
        roster_id=6,
        matchup_id=1,
        points=None,
        starters=STARTERS,
        starters_games=starters_games or {},
    )


def reconcile(
    records: tuple[LockInOpportunityRecord, ...],
    locks: dict[str, str] | None = None,
    dates: dict[str, date] | None = None,
):  # type: ignore[no-untyped-def]
    return reconcile_sleeper_locks(
        records,
        leg(locks),
        POSITIONS,
        lock_dates=dates if dates is not None else dict.fromkeys(locks or {}, GAME_DATE),
        now=NOW,
    )


def test_sleeper_lock_marks_row_locked_in_its_starting_slot() -> None:
    result = reconcile((record(),), {"p2": "sleeper-9"})

    (updated,) = result.updates
    assert updated.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
    assert updated.acknowledged_action == "locked"
    assert updated.acknowledged_at == NOW
    assert (updated.slot_index, updated.slot_position) == (2, "SF")
    assert updated.rostered_at_tipoff is True
    assert updated.next_check_at == NOW
    trace = json.loads(updated.trace_json)
    assert trace["lock_source"] == LOCK_SOURCE_SLEEPER
    assert trace["sleeper_game_id"] == "sleeper-9"
    assert trace["observed_starter"] is True
    assert result.missing_rows == () and result.vanished_locks == ()


def test_lock_matches_the_row_for_the_locked_games_eastern_date() -> None:
    earlier = record(game_id="espn-0", start=TIPOFF - timedelta(days=2))

    result = reconcile((earlier, record()), {"p2": "sleeper-9"})

    assert [row.key.game_id for row in result.updates] == ["espn-1"]


def test_ineligible_row_becomes_locked_when_sleeper_shows_a_lock() -> None:
    ineligible = record(
        status=LockInOpportunityStatus.INELIGIBLE,
        rostered_at_tipoff=False,
        tipoff_unverified=True,
        trace_json=json.dumps({"eligibility_forfeited": True}),
    )

    (updated,) = reconcile((ineligible,), {"p2": "sleeper-9"}).updates

    assert updated.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
    assert updated.rostered_at_tipoff is True
    assert updated.tipoff_unverified is False
    assert json.loads(updated.trace_json)["eligibility_forfeited"] is False


def test_lock_without_row_is_reported_missing() -> None:
    result = reconcile((record(),), {"p2": "sleeper-9", "p5": "sleeper-7"})

    assert [row.key.player_id for row in result.updates] == ["p2"]
    assert result.missing_rows == (("p5", "sleeper-7"),)


def test_lock_without_a_known_game_date_is_reported_missing() -> None:
    result = reconcile((record(),), {"p2": "sleeper-9"}, dates={})

    assert result.updates == ()
    assert result.missing_rows == (("p2", "sleeper-9"),)


def test_vanished_sleeper_lock_stays_locked_and_is_reported() -> None:
    synced = record(
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        acknowledged_action="locked",
        trace_json=json.dumps({"lock_source": LOCK_SOURCE_SLEEPER}),
    )

    result = reconcile((synced,))

    assert result.updates == ()
    assert result.vanished_locks == (("p2", "espn-1"),)


def test_unconfirmed_manual_lock_is_cleared() -> None:
    manual = record(
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        acknowledged_action="locked",
        acknowledged_at=TIPOFF,
    )

    (updated,) = reconcile((manual,)).updates

    assert updated.status is LockInOpportunityStatus.FINALIZING
    assert updated.acknowledged_action is None
    assert updated.acknowledged_at is None


def test_open_manual_pass_reverts_and_closed_pass_stays() -> None:
    open_pass = record(
        status=LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
        acknowledged_action="passed",
    )
    closed_pass = record(
        "p3",
        status=LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
        acknowledged_action="passed",
        deadline=NOW - timedelta(minutes=1),
    )

    result = reconcile((open_pass, closed_pass))

    (updated,) = result.updates
    assert updated.key.player_id == "p2"
    assert updated.status is LockInOpportunityStatus.FINALIZING
    assert updated.acknowledged_action is None


def test_manual_decisions_stay_after_the_week_ends() -> None:
    manual = record(
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        acknowledged_action="locked",
        fantasy_week_end=NOW,
    )

    assert reconcile((manual,)).updates == ()


def test_already_synced_lock_produces_no_update() -> None:
    synced = record(
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        acknowledged_action="locked",
        slot_index=2,
        slot_position="SF",
        trace_json=json.dumps({"lock_source": LOCK_SOURCE_SLEEPER, "sleeper_game_id": "s"}),
    )

    result = reconcile((synced,), {"p2": "sleeper-9"})

    assert result.updates == ()
    assert result.missing_rows == () and result.vanished_locks == ()
