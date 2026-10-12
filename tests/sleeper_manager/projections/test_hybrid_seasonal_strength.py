"""Let verified new-season games retire a prior that otherwise never loses strength."""

from datetime import timedelta
from math import fsum

import pytest

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_pools import seasonal_games
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, game


def test_current_games_fade_prior_strength_without_changing_current_recency() -> None:
    prior = tuple(game("p", i) for i in range(30))
    current = tuple(game("p", i + 100, start=CUTOFF - timedelta(days=i + 1)) for i in range(2))
    config = HybridProjectionConfig()
    weighted = seasonal_games(prior + current, CUTOFF, 2026, config)
    prior_mass = fsum(w for r, w in weighted if nba_season_start_year(r.game_start) == 2025)
    assert prior_mass == pytest.approx(6 * 6 / (6 + 2))
    assert tuple(w for r, w in weighted if r in current) == pytest.approx(
        tuple(w for _, w in seasonal_games(current, CUTOFF, 2026, config))
    )
    assert all(weight > 0 for _, weight in weighted)


def test_more_current_games_reduce_prior_mass_monotonically() -> None:
    prior = tuple(game("p", i) for i in range(30))
    masses = []
    for count in (0, 6, 24):
        current = tuple(
            game("p", i + 100, start=CUTOFF - timedelta(hours=i + 1)) for i in range(count)
        )
        masses.append(
            fsum(
                w
                for r, w in seasonal_games(prior + current, CUTOFF, 2026, HybridProjectionConfig())
                if r in prior
            )
        )
    assert masses == pytest.approx((6, 3, 1.2))
