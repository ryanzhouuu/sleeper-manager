from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.replay_models import ReplayPlayerGame
from sleeper_manager.decisions.lock_in import (
    LockInPolicyConfig,
    ScoreMaximizingLockInPolicy,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def player_game(
    player_id: str, game_id: str, expected: float, *, actual: float
) -> ReplayPlayerGame:
    distribution = ProjectionDistribution.from_weighted_observations(
        ((expected, 1.0),),
    )
    projection = ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=NOW - timedelta(hours=1),
        model_version="fixture",
        input_version=f"inputs-{game_id}",
        scoring_policy_version="scoring",
        distribution=distribution,
        reasons=(),
    )
    return ReplayPlayerGame(
        sleeper_id=player_id,
        provider_player_id=player_id,
        game_id=game_id,
        fantasy_team_id=1,
        rostered_at_tipoff=True,
        eligible_positions=("PG",),
        actual_score=actual,
        projection=projection,
    )


def test_policy_locks_known_high_score_and_passes_for_future_upside() -> None:
    policy = ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=20, seed=7))
    completed = player_game("p1", "g1", 10, actual=10)
    low_future = player_game("p2", "g2", 1, actual=1)
    high_future = player_game("p2", "g3", 20, actual=20)

    lock = policy.decide_after_game(
        completed,
        remaining_games=(low_future,),
        open_slots=((0, "PG"),),
        locked_slots=(),
        decision_time=NOW,
        league_id="league",
        week=1,
        roster_id=1,
    )
    passing = policy.decide_after_game(
        completed,
        remaining_games=(high_future,),
        open_slots=((0, "PG"),),
        locked_slots=(),
        decision_time=NOW,
        league_id="league",
        week=1,
        roster_id=1,
    )

    assert lock.kind == "lock"
    assert passing.kind == "pass"
