"""Timezone guards and live-vs-replay semantic agreement for planning inputs."""

from datetime import datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay.engine import ReplayConfig
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus
from sleeper_manager.backtesting.replay.models import (
    ReplayPlayerGame as ReplayPlayerGameRecord,
)
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.domain.planning import (
    ProjectionSnapshot,
)
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    LiveProjectionResult,
    PlanningInputsError,
    build_live_team_week_state,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    WINDOW_END,
    _distribution,
    _inputs,
    _profile,
)


def test_naive_decision_time_is_rejected() -> None:
    with pytest.raises(PlanningInputsError, match="timezone-aware"):
        build_live_team_week_state(_inputs(), decision_time=datetime(2026, 1, 7, 18))


def test_naive_bundle_timestamps_are_rejected() -> None:
    with pytest.raises(PlanningInputsError, match="timezone-aware"):
        FantasyWeekWindow(1, datetime(2026, 1, 5), WINDOW_END)


def test_live_and_replay_states_agree_semantically() -> None:
    decision_time = NOW + timedelta(hours=2)
    tipoff = NOW - timedelta(hours=20)

    def snapshot_for(player_id: str, game_id: str) -> ProjectionSnapshot:
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=decision_time,
            model_version="baseline-v1",
            input_version="live-inputs-v1",
            scoring_policy_version="scoring-v1",
            distribution=_distribution(),
            reasons=(),
        )

    live_inputs = _inputs(
        projections=tuple(
            LiveProjectionResult(player_id, game_id, snapshot_for(player_id, game_id), None)
            for player_id in ("p1", "p2")
            for game_id in ("g1", "g2")
        ),
    )
    live_state = build_live_team_week_state(live_inputs, decision_time=decision_time)

    replay_config = ReplayConfig(starter_slots=("PG", "UTIL"), league_id="league-1", week=1)
    replay_state = ReplayState(
        starter_slots=("PG", "UTIL"),
        games=(
            ReplayGame(
                game_id="g1",
                start_time=tipoff,
                final_time=None,
                week=1,
                team_ids=("12", "14"),
                status=ReplayGameStatus.SCHEDULED,
            ),
            ReplayGame(
                game_id="g2",
                start_time=tipoff + timedelta(days=2),
                final_time=None,
                week=1,
                team_ids=("12", "16"),
                status=ReplayGameStatus.SCHEDULED,
            ),
        ),
        player_games=(
            ReplayPlayerGameRecord(
                sleeper_id="p1",
                provider_player_id="401",
                game_id="g1",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("PG", "SG"),
                actual_score=0,
                projection=snapshot_for("p1", "g1"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p1",
                provider_player_id="401",
                game_id="g2",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("PG", "SG"),
                actual_score=0,
                projection=snapshot_for("p1", "g2"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p2",
                provider_player_id="402",
                game_id="g1",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("C",),
                actual_score=0,
                projection=snapshot_for("p2", "g1"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p2",
                provider_player_id="402",
                game_id="g2",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("C",),
                actual_score=0,
                projection=snapshot_for("p2", "g2"),
            ),
        ),
    )
    profile = _profile()
    replay_domain_state = team_week_state_from_replay(
        replay_state,
        config=replay_config,
        decision_time=decision_time,
        league_profile=profile,
        roster_player_ids=("p1", "p2"),
    )

    assert live_state.starter_slots == replay_domain_state.starter_slots
    assert live_state.roster_player_ids == replay_domain_state.roster_player_ids
    assert [
        (starter.slot_index, starter.player_id, starter.eligible_positions)
        for starter in live_state.observed_starters
    ] == [
        (starter.slot_index, starter.player_id, starter.eligible_positions)
        for starter in replay_domain_state.observed_starters
    ]
    assert {(item.sleeper_player_id, item.game_id) for item in live_state.opportunities} == {
        (item.sleeper_player_id, item.game_id) for item in replay_domain_state.opportunities
    }
    live_by_key = {
        (item.sleeper_player_id, item.game_id): item for item in live_state.opportunities
    }
    replay_by_key = {
        (item.sleeper_player_id, item.game_id): item for item in replay_domain_state.opportunities
    }
    for key, live_opportunity in live_by_key.items():
        replay_opportunity = replay_by_key[key]
        assert live_opportunity.status == replay_opportunity.status
        assert live_opportunity.eligible_slot_indices == replay_opportunity.eligible_slot_indices
        assert live_opportunity.eligible_positions == replay_opportunity.eligible_positions
        assert live_opportunity.provider_player_id == replay_opportunity.provider_player_id
    assert live_state.fixed_slots == replay_domain_state.fixed_slots
    assert live_state.passed_opportunities == replay_domain_state.passed_opportunities
    assert live_state.blocking_reasons == replay_domain_state.blocking_reasons

    assert all(lineage.source != "replay" for lineage in live_state.freshness.sources)
    assert any(lineage.source == "replay" for lineage in replay_domain_state.freshness.sources)
