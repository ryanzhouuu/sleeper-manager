"""Opening-night donor behavior and exclusion of unavailable or uncovered outcomes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import fsum

import pytest

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_pools import HybridHistoryPools, seasonal_games
from sleeper_manager.projections.hybrid_types import HybridHistory
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, game, history

CONFIG = HybridProjectionConfig()


def pools(data: HybridHistory) -> HybridHistoryPools:
    return HybridHistoryPools(
        data,
        cutoff=CUTOFF,
        game_start=TIPOFF,
        scoring_policy=ScoringPolicy(points=1),
        config=CONFIG,
    )


def test_opening_night_prior_and_no_history_distributions_are_not_zero() -> None:
    veteran = pools(history()).pool("target")
    entrant = pools(history(include_player=False)).pool("new-player")
    assert veteran.history_kind == "prior_season"
    assert entrant.history_kind == "no_history"
    assert entrant.means[0] > 0
    assert len(entrant.donor_ids) == 6
    assert fsum(veteran.weights) == pytest.approx(1)


def test_prior_season_influence_is_capped_and_does_not_decay_over_offseason() -> None:
    rows = tuple(game("p", i) for i in range(30))
    weighted = seasonal_games(rows, CUTOFF, 2026, CONFIG)
    assert fsum(weight for _, weight in weighted) == pytest.approx(6)
    assert len({weight for _, weight in weighted}) == 1
    single = seasonal_games((rows[0],), CUTOFF, 2026, CONFIG)
    assert single[0][1] == 1
    old = tuple(
        replace(
            row,
            game_start=row.game_start.replace(year=2025),
            finalized_at=row.finalized_at.replace(year=2025),
        )
        for row in rows
    )
    assert fsum(weight for _, weight in seasonal_games(old, CUTOFF, 2026, CONFIG)) == pytest.approx(
        3
    )


def test_new_season_results_adapt_without_discarding_old_outcomes() -> None:
    data = history()
    new = game("target", 99, points=80, start=datetime(2026, 10, 19, tzinfo=UTC))
    baseline = pools(data).pool("target")
    updated = pools(replace(data, observations=data.observations + (new,))).pool("target")
    assert updated.history_kind == "current_season"
    assert updated.means[0] > baseline.means[0]


def test_future_and_unfinalized_outcomes_do_not_change_selection() -> None:
    data = history()
    future = game("future", 0, start=CUTOFF + timedelta(days=1))
    pending = replace(game("pending", 0), finalized_at=CUTOFF + timedelta(seconds=1))
    clean = pools(data).pool("target")
    poisoned = pools(replace(data, observations=data.observations + (future, pending))).pool(
        "target"
    )
    assert clean == poisoned
    assert clean == pools(replace(data, observations=tuple(reversed(data.observations)))).pool(
        "target"
    )


def test_missing_stats_do_not_become_a_rookie_prior() -> None:
    data = history()
    rows = tuple(
        replace(row, verified_fields=("points",)) if row.player_id == "target" else row
        for row in data.observations
    )
    with pytest.raises(HybridProjectionError, match="unverified_player_stats"):
        pools(replace(data, observations=rows)).pool("target")


def test_donor_players_have_equal_initial_mass_and_target_is_excluded() -> None:
    pool = pools(history(include_player=False)).pool("new-player")
    for donor in pool.donor_ids:
        assert fsum(
            w for row, w in zip(pool.samples, pool.weights, strict=True) if row.player_id == donor
        ) == pytest.approx(1 / 6)
    assert "target" not in pools(history()).pool("target").donor_ids


def test_nonzero_foul_rules_require_verified_foul_fields() -> None:
    data = history()
    rows = tuple(
        replace(
            row,
            verified_fields=tuple(
                name for name in row.verified_fields if name != "technical_fouls"
            ),
        )
        for row in data.observations
    )
    index = HybridHistoryPools(
        replace(data, observations=rows),
        cutoff=CUTOFF,
        game_start=TIPOFF,
        scoring_policy=ScoringPolicy(points=1, technical_foul=-1),
        config=CONFIG,
    )
    assert len(index.excluded_games) == len(rows)
    with pytest.raises(HybridProjectionError, match="unverified_player_stats"):
        index.pool("target")
