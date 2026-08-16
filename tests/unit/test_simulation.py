from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay_models import ReplayPlayerGame
from sleeper_manager.decisions.simulation import SimulationError, generate_scenarios
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def test_scenarios_are_seeded_and_reject_future_projections() -> None:
    distribution = ProjectionDistribution.from_weighted_observations(((10, 1.0),))
    projection = ProjectionSnapshot(
        player_id="p1",
        game_id="g1",
        available_as_of=NOW - timedelta(hours=1),
        model_version="fixture",
        input_version="inputs-g1",
        scoring_policy_version="scoring",
        distribution=distribution,
        reasons=(),
    )
    record = ReplayPlayerGame("p1", "p1", "g1", 1, True, ("PG",), 10, projection)
    first = generate_scenarios((record,), decision_time=NOW, count=5, seed=11)
    second = generate_scenarios((record,), decision_time=NOW, count=5, seed=11)
    assert first == second

    future_projection = ProjectionSnapshot(
        player_id="p2",
        game_id="g2",
        available_as_of=NOW + timedelta(minutes=1),
        model_version="fixture",
        input_version="future",
        scoring_policy_version="scoring",
        distribution=distribution,
        reasons=(),
    )
    future = ReplayPlayerGame("p2", "p2", "g2", 1, True, ("PG",), 10, future_projection)
    with pytest.raises(SimulationError, match="not available"):
        generate_scenarios((future,), decision_time=NOW, count=1, seed=11)
