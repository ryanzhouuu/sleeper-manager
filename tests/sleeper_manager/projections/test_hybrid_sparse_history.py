"""Sparse own history must not invalidate an otherwise supported donor distribution."""

from dataclasses import replace
from datetime import timedelta
from math import fsum

import pytest

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_fitting import weight_support
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch
from sleeper_manager.projections.hybrid_pools import HybridHistoryPools
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, game, history


@pytest.mark.parametrize("count", [0, 1, 2, 4, 8])
def test_first_games_preserve_supported_projection_and_own_recency(count: int) -> None:
    data = history(include_player=False)
    own = tuple(
        game("target", i, points=80, start=CUTOFF - timedelta(days=i + 1)) for i in range(count)
    )
    data = replace(data, observations=data.observations + own)
    prepared = HybridProjectionBatch(
        data, cutoff=CUTOFF, game_start=TIPOFF, scoring_policy=ScoringPolicy(points=1)
    )
    result = prepared.project(LiveProjectionTarget("target", "next", TIPOFF, "target"))
    assert result.failure is None
    assert result.projection is not None
    assert result.maximum_weight <= 0.1 + 1e-8
    assert result.effective_sample >= 20
    pool = prepared.pools.pool("target")
    weights = {
        row.game_id: w
        for row, w in zip(pool.samples, pool.weights, strict=True)
        if row.player_id == "target"
    }
    assert len(weights) == count
    assert all(w > 0 for w in weights.values())
    raw = dict((row.game_id, w) for row, w in prepared.pools.games.get("target", ()))
    if count > 1:
        assert weights[own[0].game_id] / weights[own[1].game_id] == pytest.approx(
            raw[own[0].game_id] / raw[own[1].game_id]
        )
    if count == 1:
        assert weights[own[0].game_id] == pytest.approx(0.1)
    donor_masses = [
        fsum(w for row, w in zip(pool.samples, pool.weights, strict=True) if row.player_id == p)
        for p in pool.donor_ids
    ]
    assert max(donor_masses) == pytest.approx(min(donor_masses))


def test_supported_veteran_pool_keeps_existing_season_and_donor_masses() -> None:
    pools = HybridHistoryPools(
        history(),
        cutoff=CUTOFF,
        game_start=TIPOFF,
        scoring_policy=ScoringPolicy(points=1),
        config=HybridProjectionConfig(),
    )
    pool = pools.pool("target")
    own_mass = fsum(
        w for row, w in zip(pool.samples, pool.weights, strict=True) if row.player_id == "target"
    )
    assert own_mass == pytest.approx(6 / 11)
    assert weight_support(pool.weights, pools.config) is None


def test_sparse_history_without_donors_still_fails_support() -> None:
    data = history(include_player=False)
    data = replace(data, observations=(game("target", 0, start=CUTOFF - timedelta(days=1)),))
    prepared = HybridProjectionBatch(
        data, cutoff=CUTOFF, game_start=TIPOFF, scoring_policy=ScoringPolicy(points=1)
    )
    result = prepared.project(LiveProjectionTarget("target", "next", TIPOFF, "target"))
    assert result.failure == "insufficient_effective_sample"
    assert result.projection is None
