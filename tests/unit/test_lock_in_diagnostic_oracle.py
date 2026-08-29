"""Coverage for temporal feasibility of historical Lock-In oracle selections."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_oracle import (
    oracle_feasibility_checks,
)
from sleeper_manager.backtesting.replay.models import (
    LockedSlot,
    ReplayDecision,
    ReplayGame,
    TeamWeekReplayResult,
)


def test_oracle_feasibility_rejects_earlier_pick_finalized_after_next_game() -> None:

    team_week = _team_week()
    games = list(team_week.games)
    g1 = games[0]
    games[0] = ReplayGame(
        g1.game_id,
        g1.start_time,
        BASE + timedelta(hours=13),
        g1.week,
        g1.team_ids,
        g1.status,
    )
    team_week = replace(team_week, games=tuple(games))
    oracle = TeamWeekReplayResult(
        league_id=team_week.league_id,
        week=team_week.week,
        roster_id=team_week.roster_id,
        policy_name="oracle",
        realized_score=50,
        decisions=(
            ReplayDecision(
                BASE + timedelta(hours=13),
                "oracle_select",
                "p1",
                "g1",
                0,
                "realized-outcomes",
                50,
                50,
                "Constrained maximum-weight realized assignment.",
            ),
        ),
        locked_slots=(LockedSlot(0, "UTIL", "p1", "g1", 50, BASE + timedelta(hours=13)),),
        automatic_final_scores=(("p2", 8.0),),
        eligibility_quality=team_week.eligibility_quality.value,
        data_quality="partial",
    )

    checks = oracle_feasibility_checks(oracle, team_week)

    assert any(not check.feasible and check.kind == "lockable_earlier" for check in checks)
