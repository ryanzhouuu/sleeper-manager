from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import isfinite, sqrt

from sleeper_manager.backtesting.models import (
    BacktestObservation,
    CohortDiagnostics,
    ParticipationCalibrationBin,
)
from sleeper_manager.backtesting.validation_metrics import (
    _observation_index,
    block_bootstrap_mae_delta,
    segment_comparisons,
)
from sleeper_manager.backtesting.validation_models import (
    ComponentGateConfig,
    DevelopmentDecision,
    FoldResult,
    GateResult,
    PromotionDecision,
    PromotionGateConfig,
    _AggregateMetrics,
)
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureDataset


def evaluate_component_gates(
    diagnostics: CohortDiagnostics,
    *,
    config: ComponentGateConfig | None = None,
) -> tuple[GateResult, ...]:
    """Gate one cohort's component decomposition against its frozen prior-only control.

    A missing or non-finite candidate component is a hard failure, independent of the
    regression comparison -- it is never imputed or silently skipped.
    """
    gate = config or ComponentGateConfig()
    return (
        GateResult(
            "component_output_present",
            diagnostics.minutes_mae is not None
            and diagnostics.rate_mae is not None
            and diagnostics.participation_brier is not None,
            f"minutes={diagnostics.minutes_mae}; rate={diagnostics.rate_mae}; "
            f"participation={diagnostics.participation_brier}",
        ),
        _component_non_regression_gate(
            "minutes_non_regression", diagnostics.minutes_mae, diagnostics.control_minutes_mae, gate
        ),
        _component_non_regression_gate(
            "rate_non_regression", diagnostics.rate_mae, diagnostics.control_rate_mae, gate
        ),
        _participation_calibration_gate(diagnostics.participation_calibration, gate),
    )


def _component_non_regression_gate(
    name: str,
    candidate_mae: float | None,
    control_mae: float | None,
    gate: ComponentGateConfig,
) -> GateResult:
    if candidate_mae is None or not isfinite(candidate_mae):
        return GateResult(name, False, "candidate component output is missing or non-finite")
    if control_mae is None or not isfinite(control_mae):
        return GateResult(name, False, "control component output is missing or non-finite")
    passed = (
        candidate_mae == 0
        if control_mae == 0
        else (candidate_mae - control_mae) / control_mae <= gate.max_regression_fraction
    )
    return GateResult(name, passed, f"candidate={candidate_mae}; control={control_mae}")


def _participation_calibration_gate(
    bins: Sequence[ParticipationCalibrationBin], gate: ComponentGateConfig
) -> GateResult:
    qualifying = tuple(
        bin_ for bin_ in bins if bin_.observation_count >= gate.min_calibration_bin_size
    )
    if not qualifying:
        return GateResult(
            "participation_calibration",
            True,
            "no bin reached the minimum observation count; gate is vacuously satisfied",
        )
    failures = tuple(
        bin_
        for bin_ in qualifying
        if bin_.observed_frequency is None
        or bin_.predicted_mean is None
        or abs(bin_.observed_frequency - bin_.predicted_mean) > gate.calibration_tolerance
    )
    return GateResult(
        "participation_calibration",
        not failures,
        f"qualifying_bins={len(qualifying)}; failures={len(failures)}",
    )


def evaluate_promotion(
    *,
    development_results: Iterable[FoldResult],
    holdout_results: Iterable[FoldResult],
    dataset: HistoricalFeatureDataset,
    reference_model: str,
    candidate_model: str,
    promotable: bool = True,
    audit_passed: bool,
    config: PromotionGateConfig | None = None,
) -> PromotionDecision:
    gate = config or PromotionGateConfig()
    development = tuple(development_results)
    holdout = tuple(holdout_results)
    holdout_reference, holdout_candidate = _aggregate_common_metrics(
        holdout,
        reference_model=reference_model,
        candidate_model=candidate_model,
        config=gate,
    )
    mae_delta = _delta(holdout_candidate.mae, holdout_reference.mae)
    mae_fraction = _improvement_fraction(holdout_candidate.mae, holdout_reference.mae)
    mae_passed = (
        mae_delta is not None
        and mae_fraction is not None
        and mae_delta <= -gate.min_mae_points
        and mae_fraction >= gate.min_mae_fraction
    )
    fold_deltas = tuple(
        comparison.mae_delta
        for result in development
        for comparison in result.report.comparisons
        if comparison.candidate_model == candidate_model
        and comparison.reference_model == reference_model
        and comparison.mae_delta is not None
    )
    majority_passed = (
        bool(fold_deltas) and sum(delta < 0 for delta in fold_deltas) > len(fold_deltas) / 2
    )
    bootstrap = block_bootstrap_mae_delta(
        development,
        reference_model=reference_model,
        candidate_model=candidate_model,
    )
    bootstrap_passed = bootstrap.upper is not None and bootstrap.upper < 0
    rmse_delta = _delta(holdout_candidate.rmse, holdout_reference.rmse)
    rmse_fraction = _regression_fraction(holdout_candidate.rmse, holdout_reference.rmse)
    rmse_passed = (
        rmse_delta is not None
        and rmse_fraction is not None
        and rmse_delta <= gate.max_rmse_points
        and rmse_fraction <= gate.max_rmse_fraction
    )
    segments = segment_comparisons(
        holdout,
        dataset=dataset,
        reference_model=reference_model,
        candidate_model=candidate_model,
    )
    segment_failures = tuple(
        segment
        for segment in segments
        if segment.conclusive
        and segment.mae_delta is not None
        and segment.reference_mae is not None
        and segment.mae_delta > gate.max_segment_mae_points
        and _regression_fraction(segment.candidate_mae, segment.reference_mae)
        > gate.max_segment_mae_fraction
    )
    intervals_passed = _interval_gate(
        holdout_reference,
        holdout_candidate,
        config=gate,
    )
    brier_deltas = tuple(
        _delta(candidate[1], reference[1])
        for reference, candidate in zip(
            holdout_reference.brier_scores,
            holdout_candidate.brier_scores,
            strict=True,
        )
    )
    comparable_brier = tuple(delta for delta in brier_deltas if delta is not None)
    brier_passed = (
        bool(comparable_brier)
        and max(comparable_brier) <= gate.max_brier_delta
        and (sum(comparable_brier) / len(comparable_brier) <= 0)
    )
    reference_coverage, candidate_coverage = _coverage_rates(
        holdout,
        reference_model=reference_model,
        candidate_model=candidate_model,
    )
    coverage_ratio = candidate_coverage / reference_coverage if reference_coverage > 0 else 0.0
    coverage_passed = coverage_ratio >= gate.min_coverage_ratio
    gates = (
        GateResult(
            "holdout_mae",
            mae_passed,
            f"delta={mae_delta}; improvement_fraction={mae_fraction}",
        ),
        GateResult(
            "development_folds",
            majority_passed,
            f"improved={sum(delta < 0 for delta in fold_deltas)}/{len(fold_deltas)}",
        ),
        GateResult(
            "bootstrap_uncertainty",
            bootstrap_passed,
            f"95% interval=({bootstrap.lower}, {bootstrap.upper})",
        ),
        GateResult(
            "rmse_non_regression",
            rmse_passed,
            f"delta={rmse_delta}; regression_fraction={rmse_fraction}",
        ),
        GateResult(
            "important_segments",
            not segment_failures,
            f"material_failures={len(segment_failures)}",
        ),
        GateResult("interval_calibration", intervals_passed, "coverage and width limits"),
        GateResult("brier_non_regression", brier_passed, f"deltas={brier_deltas}"),
        GateResult(
            "target_coverage",
            coverage_passed,
            f"candidate/reference ratio={coverage_ratio:.6f}",
        ),
        GateResult("leakage_and_lineage_audit", audit_passed, f"passed={audit_passed}"),
        GateResult("promotion_eligible_family", promotable, f"promotable={promotable}"),
    )
    if all(result.passed for result in gates):
        recommendation = "promote"
    elif mae_delta is not None and mae_delta < 0 and (majority_passed or bootstrap_passed):
        recommendation = "retain_experimental"
    else:
        recommendation = "reject"
    return PromotionDecision(candidate_model, recommendation, promotable, gates)


def evaluate_development_candidate(
    fold_results: Iterable[FoldResult],
    *,
    reference_model: str,
    candidate_model: str,
    promotion_eligible: bool = True,
    bootstrap_samples: int = 2000,
    bootstrap_seed: int = 20260813,
) -> DevelopmentDecision:
    records = tuple(fold_results)
    deltas = tuple(
        comparison.mae_delta
        for result in records
        for comparison in result.report.comparisons
        if comparison.reference_model == reference_model
        and comparison.candidate_model == candidate_model
        and comparison.mae_delta is not None
    )
    improved = sum(delta < 0 for delta in deltas)
    bootstrap = block_bootstrap_mae_delta(
        records,
        reference_model=reference_model,
        candidate_model=candidate_model,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    majority = bool(deltas) and improved > len(deltas) / 2
    uncertainty = bootstrap.upper is not None and bootstrap.upper < 0
    selected = promotion_eligible and majority and uncertainty
    evidence = (
        f"improved_folds={improved}/{len(deltas)}; "
        f"bootstrap_95=({bootstrap.lower}, {bootstrap.upper}); "
        f"promotion_eligible={promotion_eligible}"
    )
    return DevelopmentDecision(
        candidate_model,
        selected,
        promotion_eligible,
        improved,
        len(deltas),
        bootstrap,
        evidence,
    )


def _aggregate_common_metrics(
    fold_results: tuple[FoldResult, ...],
    *,
    reference_model: str,
    candidate_model: str,
    config: PromotionGateConfig,
) -> tuple[_AggregateMetrics, _AggregateMetrics]:
    reference_records: list[BacktestObservation] = []
    candidate_records: list[BacktestObservation] = []
    for result in fold_results:
        reference = _observation_index(result.report, reference_model)
        candidate = _observation_index(result.report, candidate_model)
        for key in sorted(reference.keys() & candidate.keys()):
            reference_records.append(reference[key])
            candidate_records.append(candidate[key])
    return (
        _aggregate_metrics(tuple(reference_records), config),
        _aggregate_metrics(tuple(candidate_records), config),
    )


def _aggregate_metrics(
    records: tuple[BacktestObservation, ...], config: PromotionGateConfig
) -> _AggregateMetrics:
    if not records:
        return _AggregateMetrics(
            0,
            None,
            None,
            tuple((interval, None) for interval in config.intervals),
            tuple((interval, None) for interval in config.intervals),
            tuple((threshold, None) for threshold in config.thresholds),
        )
    mae = sum(record.absolute_error for record in records) / len(records)
    rmse = sqrt(sum(record.squared_error for record in records) / len(records))
    coverage: list[tuple[tuple[int, int], float]] = []
    widths: list[tuple[tuple[int, int], float]] = []
    for interval in config.intervals:
        lower, upper = interval
        inside = 0
        interval_widths: list[float] = []
        for record in records:
            percentiles = dict(record.percentiles)
            inside += percentiles[lower] <= record.actual_score <= percentiles[upper]
            interval_widths.append(percentiles[upper] - percentiles[lower])
        coverage.append((interval, inside / len(records)))
        widths.append((interval, sum(interval_widths) / len(interval_widths)))
    brier = tuple(
        (
            threshold,
            sum(
                (
                    record.probability_of_exceeding(threshold)
                    - float(record.actual_score > threshold)
                )
                ** 2
                for record in records
            )
            / len(records),
        )
        for threshold in config.thresholds
    )
    return _AggregateMetrics(len(records), mae, rmse, tuple(coverage), tuple(widths), brier)


def _interval_gate(
    reference: _AggregateMetrics,
    candidate: _AggregateMetrics,
    *,
    config: PromotionGateConfig,
) -> bool:
    for reference_coverage, candidate_coverage, reference_width, candidate_width in zip(
        reference.interval_coverage,
        candidate.interval_coverage,
        reference.interval_width,
        candidate.interval_width,
        strict=True,
    ):
        nominal = (candidate_coverage[0][1] - candidate_coverage[0][0]) / 100
        if candidate_coverage[1] is None or abs(candidate_coverage[1] - nominal) > (
            config.interval_coverage_tolerance
        ):
            return False
        if reference_width[1] is None or candidate_width[1] is None:
            return False
        if reference_width[1] == 0:
            if candidate_width[1] > 0:
                return False
        elif candidate_width[1] / reference_width[1] - 1 > config.max_interval_width_fraction:
            return False
        if reference_coverage[0] != candidate_coverage[0]:
            return False
    return True


def _coverage_rates(
    fold_results: tuple[FoldResult, ...],
    *,
    reference_model: str,
    candidate_model: str,
) -> tuple[float, float]:
    target_count = sum(result.report.target_count for result in fold_results)
    if target_count == 0:
        return 0.0, 0.0
    reference_count = sum(
        result.report.result_for(reference_model).metrics.sample_count for result in fold_results
    )
    candidate_count = sum(
        result.report.result_for(candidate_model).metrics.sample_count for result in fold_results
    )
    return reference_count / target_count, candidate_count / target_count


def _delta(candidate: float | None, reference: float | None) -> float | None:
    if candidate is None or reference is None:
        return None
    return candidate - reference


def _improvement_fraction(candidate: float | None, reference: float | None) -> float | None:
    if candidate is None or reference is None or reference <= 0:
        return None
    return (reference - candidate) / reference


def _regression_fraction(candidate: float | None, reference: float | None) -> float:
    if candidate is None or reference is None:
        return float("inf")
    if reference <= 0:
        return 0.0 if candidate <= reference else float("inf")
    return (candidate - reference) / reference
