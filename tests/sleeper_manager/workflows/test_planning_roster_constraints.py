"""Live and replay adapters retain bench capacity separately from IR membership."""

from dataclasses import replace

from sleeper_manager.backtesting.replay import ReplayConfig
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.domain.league import RosterSlot
from sleeper_manager.workflows.planning_inputs import build_live_team_week_state
from tests.sleeper_manager.backtesting.replay.test_planning_adapter import _replay_state
from tests.sleeper_manager.workflows.planning_inputs_support import NOW, _inputs, _profile


def test_live_inputs_exclude_ir_slots_from_bench_capacity() -> None:
    profile = _profile(starter_ids=("p1", None))
    profile = replace(
        profile,
        roster_slots=(*profile.roster_slots, RosterSlot(3, "IR", False)),
        rosters=(replace(profile.rosters[0], reserve_ids=("p2",)),),
    )

    state = build_live_team_week_state(_inputs(profile), decision_time=NOW)

    assert state.bench_capacity == 1
    assert state.reserve_player_ids == ("p2",)
    assert state.roster_player_ids == ("p1", "p2")


def test_replay_uses_capacity_and_reserves_when_profile_evidence_exists() -> None:
    profile = _profile(starter_ids=("p1", None))
    profile = replace(profile, rosters=(replace(profile.rosters[0], reserve_ids=("p2",)),))
    replay = replace(_replay_state(), starter_slots=("PG", "UTIL"))

    state = team_week_state_from_replay(
        replay,
        config=ReplayConfig(starter_slots=("PG", "UTIL"), roster_id=1),
        decision_time=NOW,
        league_profile=profile,
    )

    assert state.bench_capacity == 1
    assert state.reserve_player_ids == ("p2",)


def test_replay_does_not_invent_capacity_when_profile_is_missing() -> None:
    state = team_week_state_from_replay(
        _replay_state(),
        config=ReplayConfig(starter_slots=("G",), roster_id=1),
        decision_time=NOW,
    )

    assert state.bench_capacity is None
    assert state.reserve_player_ids == ()
