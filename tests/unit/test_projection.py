from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.projection import (
    ProjectionCompatibilityError,
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)


def test_projection_distribution_summarizes_weighted_empirical_observations() -> None:
    distribution = ProjectionDistribution.from_weighted_observations(
        [(0, 1), (10, 2), (20, 1)]
    ).for_exceedance_score(10)

    assert distribution.expected_value == 10
    assert distribution.median == 10
    assert distribution.percentiles == ((10, 0), (25, 0), (50, 10), (75, 10), (90, 20))
    assert distribution.range == (0, 20)
    assert distribution.variance == 50
    assert distribution.probability_exceeding_score == 0.25
    assert distribution.probability_of_exceeding(10) == 0.25


def test_projection_distribution_rejects_invalid_observations_and_percentiles() -> None:
    with pytest.raises(ProjectionCompatibilityError):
        ProjectionDistribution.from_weighted_observations([])
    with pytest.raises(ProjectionCompatibilityError):
        ProjectionDistribution.from_weighted_observations([(10, 1)], percentiles=(50, 10))
    with pytest.raises(ProjectionCompatibilityError):
        ProjectionDistribution.from_weighted_observations([(10, 0)])


def test_projection_snapshot_is_immutable_and_keeps_reasons_and_versions() -> None:
    reason = ProjectionReason("recency", "Used recent prior games", adjustment=1.5)
    snapshot = ProjectionSnapshot(
        player_id="player-1",
        game_id="game-1",
        available_as_of=datetime(2026, 8, 10, tzinfo=UTC),
        model_version="projection-baseline-v1",
        input_version="features-v1",
        scoring_policy_version="scoring-v1",
        distribution=ProjectionDistribution.from_weighted_observations([(10, 1)]),
        reasons=(reason,),
    )

    assert snapshot.reasons[0].message == "Used recent prior games"
    with pytest.raises(AttributeError):
        snapshot.model_version = "other"  # type: ignore[misc]
