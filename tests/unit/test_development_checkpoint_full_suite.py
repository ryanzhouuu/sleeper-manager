"""Compare continuous and restored execution for the complete frozen model suite."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from checkpoint_support import POLICY, row

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.development_checkpoint import (
    build_development_checkpoint,
    restore_development_continuations,
    validate_development_checkpoint,
)
from sleeper_manager.backtesting.experiments.projection_evaluation import (
    SECONDARY_SUITE_NAMES,
    frozen_manifest,
    raw_suite,
    secondary_calibrated_suite,
)
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    evaluate_selection,
    fold_summary,
)
from sleeper_manager.backtesting.models import BacktestConfig
from sleeper_manager.backtesting.validation.folds import run_validation_folds
from sleeper_manager.backtesting.validation.models import ChronologicalFold, ComponentGateConfig
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


def _full_suite_dataset() -> HistoricalFeatureDataset:
    """Return enough same-tipoff targets to materialize both calibrated models."""
    base = datetime(2025, 10, 1, tzinfo=UTC)
    rows: list[HistoricalFeatureRow] = []
    position = 0
    for day in range(80):
        start = base + timedelta(days=day)
        for player in range(4):
            template = row(position, start)
            rows.append(
                replace(
                    template,
                    player_id=f"player-{player}",
                    game_id=f"day-{day}-player-{player}",
                    prior_games=day,
                    prior_minutes_mean=30.0 if day else None,
                    prior_minutes_last=30.0 if day else None,
                    prior_start_rate=1.0 if day else None,
                )
            )
            position += 1
    return HistoricalFeatureDataset(
        dataset_version="checkpoint-full-suite-v1",
        feature_schema_version="checkpoint-schema-v1",
        generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_versions=(),
        rows=tuple(rows),
    )


def test_checkpoint_matches_continuous_direct_and_opportunity_calibration() -> None:
    """Both calibrated models preserve locked evidence, gates, and selection exactly."""
    dataset = _full_suite_dataset()
    base = datetime(2025, 10, 1, tzinfo=UTC)
    development = ChronologicalFold(
        "full-suite-development",
        2025,
        "development",
        base,
        base + timedelta(days=69, hours=23),
    )
    locked = ChronologicalFold(
        "full-suite-locked",
        2025,
        "locked",
        base + timedelta(days=70),
        base + timedelta(days=81),
        holdout=True,
    )
    backtest_config = BacktestConfig()
    continuous_raw = raw_suite()
    continuous_secondary = secondary_calibrated_suite()
    continuous_models = (*continuous_raw, *continuous_secondary)
    manifest = frozen_manifest(
        dataset=dataset,
        scoring_policy=POLICY,
        cohort_config=CohortConfig(),
        backtest_config=backtest_config,
        component_gate=ComponentGateConfig(),
        raw_suite=continuous_raw,
        secondary_suite=continuous_secondary,
        source_revision="fixture-revision",
    )
    development_result = run_validation_folds(
        dataset,
        scoring_policy=POLICY,
        models=continuous_models,
        folds=(development,),
        config=backtest_config,
        reference_model="direct_baseline",
    )[0]
    checkpoint = build_development_checkpoint(
        manifest=manifest,
        folds=(development,),
        fold_summaries=(fold_summary(development_result),),
        models=continuous_models,
    )
    continuous_locked = run_validation_folds(
        dataset,
        scoring_policy=POLICY,
        models=continuous_models,
        folds=(locked,),
        config=backtest_config,
        reference_model="direct_baseline",
    )

    restored_models = (*raw_suite(), *secondary_calibrated_suite())
    validate_development_checkpoint(
        checkpoint,
        expected_manifest=manifest,
        expected_folds=(development,),
        expected_models=restored_models,
    )
    restore_development_continuations(checkpoint, restored_models)
    restored_locked = run_validation_folds(
        dataset,
        scoring_policy=POLICY,
        models=restored_models,
        folds=(locked,),
        config=backtest_config,
        reference_model="direct_baseline",
    )

    for model_name in SECONDARY_SUITE_NAMES:
        assert restored_locked[0].report.result_for(model_name).observations == (
            continuous_locked[0].report.result_for(model_name).observations
        )
    assert fold_summary(restored_locked[0]) == fold_summary(continuous_locked[0])
    assert evaluate_selection(restored_locked, backtest_config=backtest_config) == (
        evaluate_selection(continuous_locked, backtest_config=backtest_config)
    )
