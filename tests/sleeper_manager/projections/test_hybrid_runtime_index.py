"""Indexed preparation must preserve joint outcomes, donor population, and cutoff isolation."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_runtime_index import (
    IndexedHistoryPools,
    build_hybrid_runtime_index,
)
from sleeper_manager.projections.hybrid_types import HybridHistory, ParticipationOpportunity
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, game, history

POLICY = ScoringPolicy(points=1, rebounds=1.2, assists=1.5, double_double=3)


def mixed_history() -> HybridHistory:
    previous = history()
    current = tuple(
        game(player, i + 50, points=40 - i, start=CUTOFF - timedelta(days=i + 1))
        for player in ("target", "donor-0", "donor-1")
        for i in range(7)
    )
    return replace(
        previous,
        observations=previous.observations + current,
        opportunities=previous.opportunities
        + tuple(
            ParticipationOpportunity(
                row.player_id,
                row.game_id,
                row.game_start,
                row.finalized_at,
                row.did_play,
                "current-census",
            )
            for row in current
        ),
    )


@pytest.mark.parametrize("delay", [0, 3])
@pytest.mark.parametrize("player", ["target", "unknown"])
def test_bounded_pools_match_full_population_and_distribution(delay: int, player: str) -> None:
    data = mixed_history()
    config = HybridProjectionConfig(use_external=False)
    cutoff = CUTOFF + timedelta(days=delay)
    tipoff = TIPOFF + timedelta(days=delay)
    index = build_hybrid_runtime_index(
        data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY, config=config
    )
    population = index.population(cutoff, config)
    selected = {player, *population.donors(player)}
    observations = {
        identity: tuple(row for row in data.observations if row.player_id == identity)
        for identity in selected
    }
    pools = IndexedHistoryPools(
        index, population, observations, cutoff=cutoff, scoring_policy=POLICY, config=config
    )
    indexed = HybridProjectionBatch.from_prepared(
        cutoff=cutoff,
        season=index.season,
        scoring_policy=POLICY,
        config=config,
        pools=pools,
        participation=HybridParticipation.from_counts(
            dict(index.participation), cutoff=cutoff, config=config
        ),
        history_fingerprint=index.history_fingerprint,
    )
    original = HybridProjectionBatch(
        data, cutoff=cutoff, game_start=tipoff, scoring_policy=POLICY, config=config
    )
    target = LiveProjectionTarget("sleeper", "next", tipoff, player)
    expected, actual = original.project(target), indexed.project(target)
    assert expected.failure == actual.failure is None
    assert actual.donor_ids == expected.donor_ids
    assert actual.participation == expected.participation
    assert actual.actual_center == pytest.approx(expected.actual_center, abs=1e-10)
    assert actual.projection.distribution.expected_value == pytest.approx(
        expected.projection.distribution.expected_value, abs=1e-10
    )
    assert actual.joint_weights == expected.joint_weights
    assert len(observations) <= config.donor_count + 1
    assert len(population.profiles) == 7


def test_index_rejects_backdated_reads_and_model_mismatches() -> None:
    index = build_hybrid_runtime_index(
        history(), reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    with pytest.raises(HybridProjectionError, match="after_cutoff"):
        index.population(CUTOFF - timedelta(seconds=1), HybridProjectionConfig())
    with pytest.raises(HybridProjectionError, match="model_mismatch"):
        index.population(CUTOFF, HybridProjectionConfig(use_external=False))


def test_future_outcomes_do_not_enter_profile_preparation() -> None:
    data = history()
    future = replace(
        data.observations[0], game_id="future", finalized_at=CUTOFF + timedelta(seconds=1)
    )
    base = build_hybrid_runtime_index(
        data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    expanded = build_hybrid_runtime_index(
        replace(data, observations=(*data.observations, future)),
        reference_time=CUTOFF,
        game_start=TIPOFF,
        scoring_policy=POLICY,
    )
    assert base == expanded


def test_scoring_gaps_are_retained_and_not_replaced_by_index_defaults() -> None:
    data = history()
    data = replace(
        data,
        observations=tuple(replace(row, verified_fields=("points",)) for row in data.observations),
    )
    index = build_hybrid_runtime_index(
        data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    assert index.profiles == ()
    assert "target" in index.incomplete_players
    assert len(index.excluded_games) == len(data.observations)
