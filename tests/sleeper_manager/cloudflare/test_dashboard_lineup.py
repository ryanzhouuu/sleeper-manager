"""Dashboard lineup rows: derived game states and the planner's decision on each row."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from sleeper_manager.cloudflare.dashboard_lineup import lineup_document, plan_summary
from sleeper_manager.cloudflare.home_document import GameRow, RosterRow
from sleeper_manager.cloudflare.lineup_states import game_state
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)

NOW = datetime(2026, 11, 7, 18, tzinfo=UTC)
EARLY = datetime(2026, 11, 6, 0, 30, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)
DEADLINE = LATER - timedelta(minutes=10)


def record(
    player_id: str = "p1",
    game_id: str = "g1",
    *,
    status: Status = Status.ACTIONABLE,
    start: datetime = EARLY,
    deadline: datetime = DEADLINE,
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
        created_at=EARLY,
        updated_at=EARLY,
        rostered_at_tipoff=True,
        stable_score=41.5,
        row_version=4,
        trace_json="{}",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"status": Status.ACKNOWLEDGED_LOCKED}, "locked"),
        ({}, "lockable"),
        ({"status": Status.INELIGIBLE}, "passed_eligibility_lost"),
        ({"status": Status.EXPIRED}, "passed_window_closed"),
        ({"deadline": NOW - timedelta(minutes=1)}, "passed_window_closed"),
        ({"status": Status.INELIGIBLE, "rostered_at_tipoff": False}, "not_eligible"),
        ({"tipoff_unverified": True, "status": Status.INELIGIBLE}, "unverified"),
        ({"start": LATER, "status": Status.SCHEDULED, "stable_score": None}, "upcoming"),
        ({"status": Status.ACTIVE, "stable_score": None}, "live"),
    ],
)
def test_game_state_covers_every_derived_state(changes: dict[str, object], expected: str) -> None:
    assert game_state(record(**changes), NOW, records=()).state == expected  # type: ignore[arg-type]


def test_next_game_starting_closes_the_window() -> None:
    newer = record(game_id="g2", start=NOW - timedelta(hours=1), status=Status.ACTIVE)

    assert game_state(record(), NOW, records=(newer,)).state == "passed_window_closed"


def test_live_sleeper_lock_marks_the_game_locked_before_sync() -> None:
    state = game_state(record(), NOW, records=(), sleeper_lock_date=date(2026, 11, 5))

    assert state.state == "locked"


def test_games_after_a_players_lock_are_no_longer_lockable() -> None:
    locked = record(
        game_id="g0", start=EARLY - timedelta(days=1), status=Status.ACKNOWLEDGED_LOCKED
    )

    state = game_state(record(), NOW, records=(locked, record()))

    assert (state.state, state.deadline) == ("passed_window_closed", None)


def test_a_live_sleeper_lock_closes_the_players_other_games() -> None:
    earlier = record(game_id="g0", start=EARLY - timedelta(days=1))

    state = game_state(record(), NOW, records=(earlier,), sleeper_lock_date=date(2026, 11, 4))

    assert state.state == "passed_window_closed"


def test_lockable_game_carries_its_deadline() -> None:
    assert game_state(record(), NOW, records=()).deadline == DEADLINE


def test_only_unverified_games_are_overridable() -> None:
    unverified = record(tipoff_unverified=True)

    assert game_state(unverified, NOW, records=()).overridable is True
    assert game_state(record(), NOW, records=()).overridable is False
    locked = record(tipoff_unverified=True, status=Status.ACKNOWLEDGED_LOCKED)
    assert game_state(locked, NOW, records=()).overridable is False
    closed = record(tipoff_unverified=True, deadline=NOW - timedelta(minutes=1))
    assert game_state(closed, NOW, records=()).overridable is False


ROWS = (
    RosterRow(slot="PG", name="Ann", nba_team="NOP", espn_id="1", player_id="p1"),
    RosterRow(slot="UTIL", name="Ben", nba_team="BOS", espn_id="2", player_id="p2"),
    RosterRow(slot="BN", name="Cal", nba_team="TOR", espn_id="3", player_id="p3"),
    RosterRow(slot="IR", name="Dee", nba_team=None, espn_id=None, player_id="p4"),
)
GAMES = (
    GameRow(start=EARLY, player_name="Ann", espn_id="1", opponent="TOR", home=True, player_id="p1"),
    GameRow(
        start=LATER, player_name="Ann", espn_id="1", opponent="MIA", home=False, player_id="p1"
    ),
    GameRow(start=LATER, player_name="Cal", espn_id="3", opponent="HOU", home=True, player_id="p3"),
)


def trace(*, score_actions=(), moves=(), desired=None):  # type: ignore[no-untyped-def]
    return {
        "schema_version": 3,
        "status": "action_required",
        "score_actions": list(score_actions),
        "moves": list(moves),
        "desired_assignments": desired
        or [
            {"slot_index": 0, "slot_position": "PG", "player_id": "p1"},
            {"slot_index": 1, "slot_position": "UTIL", "player_id": "p2"},
        ],
        "observed_terminal_score": 300.0,
        "expected_terminal_score": 312.5,
        "terminal_score_p10": 290.0,
        "terminal_score_p90": 335.0,
        "confidence": "medium",
    }


def action(kind: str, player_id: str = "p1", score: float = 41.5) -> dict[str, object]:
    return {
        "player_id": player_id,
        "game_id": "g1",
        "kind": kind,
        "score": score,
        "deadline": DEADLINE.isoformat(),
        "slot_index": 0 if kind == "lock" else None,
        "slot_position": "PG" if kind == "lock" else None,
        "alternative_expected_score": 30.2,
        "confidence": 0.81,
    }


def document(plan, records=None):  # type: ignore[no-untyped-def]
    rows = (record(),) if records is None else records
    return lineup_document(ROWS, GAMES, rows, plan, now=NOW, sleeper_locks=None)


def test_lineup_orders_by_sleeper_slot_then_bench() -> None:
    rows = document(None)

    assert [(row["slot"], row["slot_index"]) for row in rows] == [
        ("PG", 0),
        ("UTIL", 1),
        ("BN", None),
        ("IR", None),
    ]
    ann = rows[0]
    assert [(game["opponent"], game["state"]) for game in ann["games"]] == [
        ("TOR", "lockable"),
        ("MIA", "upcoming"),
    ]
    assert ann["games"][0]["score"] == 41.5
    assert (
        ann["games"][0]["opponent_image_url"] == "https://a.espncdn.com/i/teamlogos/nba/500/tor.png"
    )
    assert ann["games"][0]["game_id"] == "g1"
    assert ann["games"][0]["deadline"] == DEADLINE.isoformat()
    assert ann["decision"] is None


def test_lock_action_attaches_to_its_player_row() -> None:
    rows = document(trace(score_actions=[action("lock")]))

    decision = rows[0]["decision"]
    assert decision["kind"] == "lock"
    assert decision["text"] == "Lock 41.5 vs TOR"
    assert decision["game_id"] == "g1"
    assert decision["alternative_expected"] == 30.2
    assert decision["confidence"] == 0.81
    assert decision["deadline"] == DEADLINE.isoformat()
    assert (decision["team_before"], decision["team_after"]) == (300.0, 312.5)
    assert rows[1]["decision"] is None


def test_pass_with_bench_move_is_a_sacrifice() -> None:
    plan = trace(
        score_actions=[action("pass")],
        moves=[
            {
                "kind": "swap",
                "player_id": "p3",
                "swap_player_id": "p1",
                "source_slot_index": None,
                "target_slot_index": 0,
                "deadline": DEADLINE.isoformat(),
            }
        ],
        desired=[
            {"slot_index": 0, "slot_position": "PG", "player_id": "p3"},
            {"slot_index": 1, "slot_position": "UTIL", "player_id": "p2"},
        ],
    )

    rows = document(plan)

    assert rows[0]["decision"]["kind"] == "sacrifice"
    assert rows[0]["decision"]["text"] == "Bench for Cal — gives up 41.5 vs TOR"
    assert rows[2]["decision"]["kind"] == "move"
    assert rows[2]["decision"]["text"] == "Start at PG"


def test_pass_kept_in_lineup_is_preserve_with_slot_move_text() -> None:
    plan = trace(
        score_actions=[action("pass")],
        desired=[
            {"slot_index": 0, "slot_position": "PG", "player_id": "p2"},
            {"slot_index": 1, "slot_position": "UTIL", "player_id": "p1"},
        ],
    )

    decision = document(plan)[0]["decision"]

    assert decision["kind"] == "preserve"
    assert decision["text"] == "Keep starting — move to UTIL to stay lockable"


def test_pass_kept_in_the_same_slot_is_plain_preserve() -> None:
    decision = document(trace(score_actions=[action("pass")]))[0]["decision"]

    assert (decision["kind"], decision["text"]) == ("preserve", "Keep starting to stay lockable")


def test_unverified_game_exposes_override_version() -> None:
    game = document(None, records=(record(tipoff_unverified=True),))[0]["games"][0]

    assert game["state"] == "unverified"
    assert game["overridable"] is True
    assert game["version"] == 4


def test_plan_summary_reads_before_and_after_totals() -> None:
    assert plan_summary(trace()) == {
        "status": "action_required",
        "team_before": 300.0,
        "team_after": 312.5,
        "confidence": "medium",
        "p10": 290.0,
        "p90": 335.0,
        "undecided": [],
    }
    assert plan_summary(None) == {
        "status": None,
        "team_before": None,
        "team_after": None,
        "confidence": None,
        "p10": None,
        "p90": None,
        "undecided": [],
    }


def test_plan_summary_names_undecided_finals() -> None:
    withheld = {
        **trace(),
        "expected_terminal_score": None,
        "undecided_scores": [
            {"player_id": "p1", "game_id": "g1", "pass_confidence": 0.66, "lock_confidence": 0.34},
            {"player_id": "p9", "game_id": "g2", "pass_confidence": "bad"},
        ],
    }

    summary = plan_summary(withheld, {"p1": "Ann"})

    assert summary["team_after"] is None
    assert summary["undecided"] == [
        {"name": "Ann", "pass_confidence": 0.66, "lock_confidence": 0.34},
        {"name": None, "pass_confidence": None, "lock_confidence": None},
    ]


def test_unreadable_plan_trace_yields_no_decisions() -> None:
    rows = document({"schema_version": 3, "score_actions": "bad"})

    assert all(row["decision"] is None for row in rows)
    assert json.dumps(rows)
