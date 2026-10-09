import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.decisions.simulation import (
    ScenarioInput,
    SimulationError,
    generate_projection_scenarios,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


@pytest.mark.parametrize("seed", [0, 11, 73])
def test_prepared_sampling_matches_original_draws_with_large_and_empty_support(seed: int) -> None:
    """Keep seeded results exact across empirical distributions and mean-only fallbacks."""
    empirical = ProjectionDistribution.from_weighted_observations(
        tuple((float(i % 101 - 20), (i % 7 + 1) / 8000) for i in range(2000))
    )
    mean_only = replace(empirical, weighted_observations=())
    records = tuple(
        ScenarioInput(
            player,
            player,
            "game",
            ("PG",),
            ProjectionSnapshot(player, "game", NOW, "fixture", player, "scoring", distribution, ()),
        )
        for player, distribution in (("c", empirical), ("b", mean_only), ("a", empirical))
    )
    randomizer = random.Random(seed)
    expected = []
    for _ in range(50):
        values = []
        for record in sorted(records, key=lambda r: r.candidate_id):
            observations = record.projection.distribution.weighted_observations
            score = record.projection.distribution.expected_value
            if observations:
                score = randomizer.choices(
                    tuple(v for v, _ in observations),
                    weights=tuple(w for _, w in observations),
                    k=1,
                )[0]
            values.append((record.candidate_id, score))
        expected.append(tuple(values))
    result = generate_projection_scenarios(records, decision_time=NOW, count=50, seed=seed)
    assert [scenario.values for scenario in result] == expected


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
