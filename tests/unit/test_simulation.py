from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.decisions.simulation import (
    ScenarioInput,
    SimulationError,
    generate_projection_scenarios,
)
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
    record = ScenarioInput("p1:g1", "p1", "g1", ("PG",), projection)
    first = generate_projection_scenarios((record,), decision_time=NOW, count=5, seed=11)
    second = generate_projection_scenarios((record,), decision_time=NOW, count=5, seed=11)
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
    future = ScenarioInput("p2:g2", "p2", "g2", ("PG",), future_projection)
    with pytest.raises(SimulationError, match="not available"):
        generate_projection_scenarios((future,), decision_time=NOW, count=1, seed=11)
