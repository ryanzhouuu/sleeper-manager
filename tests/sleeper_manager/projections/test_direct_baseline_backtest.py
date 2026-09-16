"""Backtest parity, prefix checks, and cached fingerprints for direct baseline."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sleeper_manager.backtesting import BacktestConfig, BacktestModel, run_backtest
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
)
from sleeper_manager.projections.direct_baseline_history import (
    _DirectBaselineHistoryIndex,
    _row_fingerprint,
)
from tests.sleeper_manager.projections.direct_baseline_incremental_support import (
    BASE,
    POLICY,
    ExplicitHistoryProjector,
    feature_dataset,
    row,
)


def test_backtest_reports_match_explicit_reference_for_raw_and_calibrated_direct_models() -> None:
    """Preserve observations, skips, cohorts, metrics, and comparisons end to end."""
    rows = tuple(
        row(f"{player}-g{day}", player, BASE + timedelta(days=day), 10 + day * 2 + offset)
        for day in range(5)
        for player, offset in (("p1", 0), ("p2", 5))
    )
    dataset = feature_dataset(rows)
    config = BacktestConfig(min_prior_games=2)
    incremental = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", DirectFantasyPointBaseline()),
            BacktestModel(
                "calibrated",
                CalibratedProjectionModel(
                    DirectFantasyPointBaseline(), min_samples=1, refresh_interval=1
                ),
            ),
        ),
        config=config,
    )
    explicit = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", ExplicitHistoryProjector()),
            BacktestModel(
                "calibrated",
                CalibratedProjectionModel(
                    ExplicitHistoryProjector(), min_samples=1, refresh_interval=1
                ),
            ),
        ),
        config=config,
    )

    assert incremental.dataset_version == explicit.dataset_version
    assert incremental.scoring_policy_version == explicit.scoring_policy_version
    assert incremental.config_version == explicit.config_version
    assert incremental.target_count == explicit.target_count
    assert incremental.target_skips == explicit.target_skips
    assert incremental.reference_model == explicit.reference_model
    assert incremental.comparisons == explicit.comparisons
    for incremental_result, explicit_result in zip(
        incremental.model_results,
        explicit.model_results,
        strict=True,
    ):
        assert incremental_result.model.name == explicit_result.model.name
        assert incremental_result.observations == explicit_result.observations
        assert incremental_result.skips == explicit_result.skips
        assert incremental_result.metrics == explicit_result.metrics
        assert incremental_result.cohort_diagnostics == explicit_result.cohort_diagnostics


def history_index(rows: Sequence[HistoricalFeatureRow]) -> _DirectBaselineHistoryIndex:
    """Commit fixture rows to a direct history index in chronological order."""
    index = _DirectBaselineHistoryIndex(scoring_policy=POLICY, half_life_days=14.0)
    observations = tuple(DirectBaselineObservation.from_historical_row(item) for item in rows)
    index.extend(observations, len(observations))
    return index


def reference_all_finalized(
    index: _DirectBaselineHistoryIndex, count: int, available_as_of: datetime
) -> bool:
    """Recompute prefix availability with the explicit scan the index replaces."""
    return all(
        item.outcome_finalized_at is None or item.outcome_finalized_at <= available_as_of
        for item in index.rows[:count]
    )


def test_finalized_prefix_check_matches_explicit_scan() -> None:
    """Keep the O(1) finalization gate exact across finalization shapes."""
    starts = [BASE + timedelta(hours=6 * position) for position in range(6)]
    rows = (
        row("g0", "p1", starts[0], 10),
        row("g1", "p1", starts[1], 12, omit_finalization=True),
        row("g2", "p1", starts[2], 14, finalized_at=starts[2] + timedelta(hours=1)),
        row("g3", "p1", starts[3], 16, finalized_at=starts[2] + timedelta(hours=1)),
        row("g4", "p1", starts[4], 18, finalized_at=starts[4] + timedelta(hours=5)),
        row("g5", "p1", starts[5], 20),
    )
    index = history_index(rows)
    cutoffs = (
        starts[0] - timedelta(hours=1),
        starts[2] + timedelta(hours=1),
        starts[4] + timedelta(hours=5),
        starts[5] + timedelta(hours=3),
    )
    for count in range(len(rows) + 1):
        for cutoff in cutoffs:
            assert index._all_finalized_by(count, cutoff) == reference_all_finalized(
                index, count, cutoff
            )


def test_historical_row_compaction_reuses_cached_observations() -> None:
    """Avoid recompacting identical boundary rows on every incremental sync."""
    first = row("g0", "p1", BASE, 10)
    equal = row("g0", "p1", BASE, 10)
    changed = row("g0", "p1", BASE, 12)

    assert hash(first) == hash(equal)
    assert DirectBaselineObservation.from_historical_row(first) is (
        DirectBaselineObservation.from_historical_row(equal)
    )
    assert DirectBaselineObservation.from_historical_row(first) != (
        DirectBaselineObservation.from_historical_row(changed)
    )


def test_row_fingerprint_reuses_cached_digests() -> None:
    """Keep provenance digests stable without re-serializing identical rows."""
    observation = DirectBaselineObservation.from_historical_row(row("g0", "p1", BASE, 10))
    _row_fingerprint.cache_clear()

    assert _row_fingerprint(observation) == _row_fingerprint(observation)
    assert _row_fingerprint.cache_info().hits == 1
