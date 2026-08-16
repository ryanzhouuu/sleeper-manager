import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.backtesting import COHORT_NAMES, CohortConfig, run_backtest
from sleeper_manager.backtesting.experiment import (
    PHASE4_DIRECT_BASELINE,
    PHASE4_OPPORTUNITY_FULL,
    PHASE4_RAW_SUITE_NAMES,
    PHASE4_SECONDARY_SUITE_NAMES,
    Phase4ValidationError,
    _json_value,
    assert_phase4_manifest_frozen,
    evaluate_phase4_selection,
    freeze_phase4_manifest,
    phase4_frozen_manifest,
    phase4_markdown_report,
    phase4_raw_suite,
    phase4_report,
    phase4_secondary_calibrated_suite,
)
from sleeper_manager.backtesting.models import BacktestConfig
from sleeper_manager.backtesting.validation import (
    ChronologicalFold,
    ComponentGateConfig,
    run_validation_folds,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 16, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


def row(game_id: str, player_id: str, start: datetime, points: int) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="phase4-fixture",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
        game_id=game_id,
        game_start=start,
        team_id="CHI",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.AVAILABLE,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=28,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
        opponent_defensive_rating=98.0,
        league_defensive_rating=100.0,
        opponent_sample_size=8,
        pace_factor=1.02,
    )


def small_fixture_dataset() -> HistoricalFeatureDataset:
    base = datetime(2025, 11, 1, tzinfo=UTC)
    rows = []
    for player_index in range(3):
        player_id = f"player-{player_index}"
        for day in range(4):
            rows.append(row(f"{player_id}-g{day}", player_id, base + timedelta(days=day), 10 + day))
    return HistoricalFeatureDataset(
        dataset_version="phase4-fixture-v1",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 16, tzinfo=UTC),
        source_versions=(),
        rows=tuple(rows),
    )


def test_phase4_raw_suite_names_and_order_are_deterministic() -> None:
    first = phase4_raw_suite()
    second = phase4_raw_suite()

    assert tuple(model.name for model in first) == PHASE4_RAW_SUITE_NAMES
    assert tuple(model.name for model in second) == PHASE4_RAW_SUITE_NAMES
    assert len(set(PHASE4_RAW_SUITE_NAMES)) == len(PHASE4_RAW_SUITE_NAMES)


def test_phase4_secondary_suite_never_includes_calibrated_ablations() -> None:
    secondary = phase4_secondary_calibrated_suite()
    names = tuple(model.name for model in secondary)

    assert names == PHASE4_SECONDARY_SUITE_NAMES
    assert not any("no_pace" in name or "no_defense" in name for name in names)


def test_phase4_manifest_mismatch_fails_before_locked_retrospective_evaluation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    manifest_path = tmp_path / "phase4-manifest.json"
    frozen = {"dataset_version": "a", "scoring_policy_version": "1"}

    with pytest.raises(Phase4ValidationError):
        assert_phase4_manifest_frozen(manifest_path, frozen)

    freeze_phase4_manifest(manifest_path, frozen)
    assert_phase4_manifest_frozen(manifest_path, frozen)  # matches, does not raise

    drifted = {"dataset_version": "b", "scoring_policy_version": "1"}
    with pytest.raises(Phase4ValidationError):
        assert_phase4_manifest_frozen(manifest_path, drifted)


def test_phase4_frozen_manifest_includes_candidate_cohort_metric_and_gate_configuration() -> None:
    dataset = small_fixture_dataset()
    raw_suite = phase4_raw_suite()
    secondary_suite = phase4_secondary_calibrated_suite()

    manifest = phase4_frozen_manifest(
        dataset=dataset,
        scoring_policy=POLICY,
        cohort_config=CohortConfig(),
        backtest_config=BacktestConfig(),
        component_gate=ComponentGateConfig(),
        raw_suite=raw_suite,
        secondary_suite=secondary_suite,
    )

    assert manifest["dataset_version"] == dataset.dataset_version
    assert manifest["scoring_policy_version"] == POLICY.version
    assert manifest["cohort_config_version"] == CohortConfig().version
    assert manifest["backtest_config_version"] == BacktestConfig().version
    assert manifest["component_gate"]["max_regression_fraction"] == 0.02
    assert len(manifest["raw_suite"]) == len(raw_suite)
    assert len(manifest["secondary_suite"]) == len(secondary_suite)
    assert manifest["selection_rule"]["max_mae_delta"] == 0.0


def _locked_retrospective_fold_results(dataset: HistoricalFeatureDataset) -> tuple:  # type: ignore[type-arg]
    fold = ChronologicalFold(
        name="fixture-locked-retrospective",
        season_start=2025,
        phase="full",
        start_at=datetime(2025, 11, 1, tzinfo=UTC),
        end_at=datetime(2025, 11, 10, tzinfo=UTC),
        holdout=True,
    )
    return run_validation_folds(
        dataset,
        scoring_policy=POLICY,
        models=phase4_raw_suite(),
        folds=(fold,),
        config=BacktestConfig(),
        reference_model=PHASE4_DIRECT_BASELINE,
    )


def _build_report(dataset: HistoricalFeatureDataset, generated_at: datetime) -> dict:  # type: ignore[type-arg]
    locked_retrospective_results = _locked_retrospective_fold_results(dataset)
    manifest = phase4_frozen_manifest(
        dataset=dataset,
        scoring_policy=POLICY,
        cohort_config=CohortConfig(),
        backtest_config=BacktestConfig(),
        component_gate=ComponentGateConfig(),
        raw_suite=phase4_raw_suite(),
        secondary_suite=phase4_secondary_calibrated_suite(),
    )
    return phase4_report(
        generated_at=generated_at,
        manifest=manifest,
        manifest_path=Path("phase4-manifest.json"),
        dataset=dataset,
        scoring_policy=POLICY,
        backtest_config=BacktestConfig(),
        component_gate_config=ComponentGateConfig(),
        development_results=(),
        locked_retrospective_results=locked_retrospective_results,
    )


def test_phase4_selection_decision_reports_gate_status_and_is_provisional() -> None:
    dataset = small_fixture_dataset()
    decision = evaluate_phase4_selection(
        _locked_retrospective_fold_results(dataset),
        backtest_config=BacktestConfig(),
        component_gate_config=ComponentGateConfig(),
    )

    assert decision.reference_model == PHASE4_DIRECT_BASELINE
    assert decision.candidate_model == PHASE4_OPPORTUNITY_FULL
    assert decision.selected_model in (PHASE4_DIRECT_BASELINE, PHASE4_OPPORTUNITY_FULL)
    assert decision.provisional is True
    assert decision.invariants_passed is True


def test_phase4_json_report_contains_every_required_field_and_cohort() -> None:
    generated_at = datetime(2026, 8, 16, 12, tzinfo=UTC)
    report = _build_report(small_fixture_dataset(), generated_at)

    assert report["generated_at"] == generated_at
    modeled = report["modeled"]
    assert modeled["locked_evaluation"]["label"] == "locked_retrospective"
    assert "previously inspected" in modeled["locked_evaluation"]["note"]
    assert modeled["selection"].candidate_model == PHASE4_OPPORTUNITY_FULL
    assert modeled["dataset"]["dataset_version"] == small_fixture_dataset().dataset_version
    assert modeled["scoring_policy_version"] == POLICY.version
    assert len(modeled["locked_retrospective_folds"]) == 1
    fold_summary = modeled["locked_retrospective_folds"][0]
    assert fold_summary["evidence_label"] == "locked_retrospective"
    candidate_diagnostics = fold_summary["models"][PHASE4_OPPORTUNITY_FULL]["cohort_diagnostics"]
    assert tuple(candidate_diagnostics.keys()) == COHORT_NAMES
    assert modeled["limitations"]


def test_phase4_markdown_report_renders_required_tables_and_selection_result() -> None:
    generated_at = datetime(2026, 8, 16, 12, tzinfo=UTC)
    report = _build_report(small_fixture_dataset(), generated_at)
    markdown = phase4_markdown_report(report)

    assert "# Phase 4 Validation Closure Report" in markdown
    assert f"`{report['modeled']['selection'].selected_model}`" in markdown
    assert "## Selection decision" in markdown
    assert "## Locked retrospective folds" in markdown
    assert "## Cohort coverage and full-mixture metrics" in markdown
    for cohort_name in COHORT_NAMES:
        assert cohort_name in markdown
    assert "## Limitations" in markdown


def test_phase4_report_preserves_original_target_denominators() -> None:
    dataset = small_fixture_dataset()
    locked_retrospective_results = _locked_retrospective_fold_results(dataset)
    report = _build_report(dataset, datetime(2026, 8, 16, 12, tzinfo=UTC))

    fold_result = locked_retrospective_results[0]
    fold_summary = report["modeled"]["locked_retrospective_folds"][0]
    assert fold_summary["target_count"] == fold_result.report.target_count
    for model_name, model_summary in fold_summary["models"].items():
        underlying = fold_result.report.result_for(model_name)
        for diagnostic in underlying.cohort_diagnostics:
            reported = model_summary["cohort_diagnostics"][diagnostic.cohort]
            assert reported.target_count == diagnostic.target_count
            assert reported.skip_reasons == diagnostic.skip_reasons


def test_phase4_modeled_report_data_is_byte_equivalent_across_identical_cached_runs() -> None:
    dataset = small_fixture_dataset()
    report_a = _build_report(dataset, datetime(2026, 8, 16, 12, tzinfo=UTC))
    report_b = _build_report(dataset, datetime(2026, 8, 17, 3, tzinfo=UTC))

    assert report_a["generated_at"] != report_b["generated_at"]
    assert _json_round_trip(report_a["modeled"]) == _json_round_trip(report_b["modeled"])


def _json_round_trip(value: object) -> object:
    return json.loads(json.dumps(_json_value(value), sort_keys=True))


def test_phase4_report_does_not_copy_private_raw_values() -> None:
    dataset = small_fixture_dataset()
    report = _build_report(dataset, datetime(2026, 8, 16, 12, tzinfo=UTC))
    serialized = json.dumps(_json_round_trip(report["modeled"]))

    # Real per-target identifiers and raw box-score values never appear in the report --
    # only aggregate counts, versions, and cohort-level diagnostics do.
    for player_id in {row.player_id for row in dataset.rows}:
        assert player_id not in serialized
    for game_id in {row.game_id for row in dataset.rows}:
        assert game_id not in serialized


def test_phase4_small_fixture_runs_all_candidates_through_every_cohort_metric() -> None:
    dataset = small_fixture_dataset()
    report = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=phase4_raw_suite(),
        reference_model="direct_baseline",
    )

    assert tuple(result.model.name for result in report.model_results) == PHASE4_RAW_SUITE_NAMES
    for result in report.model_results:
        cohorts_present = tuple(diagnostic.cohort for diagnostic in result.cohort_diagnostics)
        assert cohorts_present == COHORT_NAMES
        for diagnostic in result.cohort_diagnostics:
            assert diagnostic.full_mixture is not None
            assert diagnostic.target_count >= diagnostic.successful_count
