from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite, sqrt
from random import Random

from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestModel,
    BacktestObservation,
    BacktestReport,
    CohortDiagnostics,
    ParticipationCalibrationBin,
)
from sleeper_manager.backtesting.runner import run_backtest
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


@dataclass(frozen=True, slots=True)
class ChronologicalFold:
    name: str
    season_start: int
    phase: str
    start_at: datetime
    end_at: datetime
    holdout: bool = False


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: ChronologicalFold
    report: BacktestReport


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    metric: str
    candidate_model: str
    reference_model: str
    sample_count: int
    seed: int
    lower: float | None
    upper: float | None


@dataclass(frozen=True, slots=True)
class SegmentComparison:
    segment: str
    value: str
    candidate_model: str
    reference_model: str
    player_game_count: int
    game_count: int
    conclusive: bool
    reference_mae: float | None
    candidate_mae: float | None
    mae_delta: float | None


@dataclass(frozen=True, slots=True)
class PromotionGateConfig:
    min_mae_points: float = 0.25
    min_mae_fraction: float = 0.01
    max_rmse_points: float = 0.25
    max_rmse_fraction: float = 0.01
    max_segment_mae_points: float = 0.5
    max_segment_mae_fraction: float = 0.02
    interval_coverage_tolerance: float = 0.05
    max_interval_width_fraction: float = 0.05
    max_brier_delta: float = 0.005
    min_coverage_ratio: float = 0.95
    intervals: tuple[tuple[int, int], ...] = ((10, 90), (25, 75))
    thresholds: tuple[float, ...] = (20.0, 30.0, 40.0, 50.0, 60.0)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ComponentGateConfig:
    max_regression_fraction: float = 0.02
    min_calibration_bin_size: int = 100
    calibration_tolerance: float = 0.05


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


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    candidate_model: str
    recommendation: str
    promotable: bool
    gates: tuple[GateResult, ...]


@dataclass(frozen=True, slots=True)
class DevelopmentDecision:
    candidate_model: str
    selected: bool
    promotion_eligible: bool
    improved_folds: int
    fold_count: int
    bootstrap: BootstrapInterval
    evidence: str


@dataclass(frozen=True, slots=True)
class _AggregateMetrics:
    sample_count: int
    mae: float | None
    rmse: float | None
    interval_coverage: tuple[tuple[tuple[int, int], float | None], ...]
    interval_width: tuple[tuple[tuple[int, int], float | None], ...]
    brier_scores: tuple[tuple[float, float | None], ...]


def regular_season_folds(
    *,
    development_seasons: tuple[int, ...] = (2022, 2023, 2024),
    holdout_season: int = 2025,
) -> tuple[ChronologicalFold, ...]:
    folds: list[ChronologicalFold] = []
    for season_start in development_seasons + (holdout_season,):
        holdout = season_start == holdout_season
        windows = (
            (
                "early",
                datetime(season_start, 10, 1, tzinfo=UTC),
                datetime(season_start + 1, 1, 1, tzinfo=UTC),
            ),
            (
                "middle",
                datetime(season_start + 1, 1, 1, tzinfo=UTC),
                datetime(season_start + 1, 3, 1, tzinfo=UTC),
            ),
            (
                "late",
                datetime(season_start + 1, 3, 1, tzinfo=UTC),
                datetime(season_start + 1, 5, 1, tzinfo=UTC),
            ),
        )
        for phase, start_at, next_start in windows:
            folds.append(
                ChronologicalFold(
                    name=f"{season_start}-{str(season_start + 1)[-2:]}-{phase}",
                    season_start=season_start,
                    phase=phase,
                    start_at=start_at,
                    end_at=next_start - timedelta(microseconds=1),
                    holdout=holdout,
                )
            )
    return tuple(folds)


def run_validation_folds(
    dataset: HistoricalFeatureDataset,
    *,
    scoring_policy: ScoringPolicy,
    models: Iterable[BacktestModel],
    folds: Iterable[ChronologicalFold],
    config: BacktestConfig | None = None,
    reference_model: str | None = None,
) -> tuple[FoldResult, ...]:
    base = config or BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    records = tuple(models)
    return tuple(
        FoldResult(
            fold,
            run_backtest(
                dataset,
                scoring_policy=scoring_policy,
                models=records,
                config=replace(base, start_at=fold.start_at, end_at=fold.end_at),
                reference_model=reference_model,
            ),
        )
        for fold in folds
    )


def block_bootstrap_mae_delta(
    fold_results: Iterable[FoldResult],
    *,
    reference_model: str,
    candidate_model: str,
    samples: int = 2000,
    seed: int = 20260813,
) -> BootstrapInterval:
    if samples <= 0:
        raise ValueError("Bootstrap sample count must be positive")
    blocks = _paired_error_blocks(
        fold_results,
        reference_model=reference_model,
        candidate_model=candidate_model,
    )
    if not blocks:
        return BootstrapInterval(
            "mae_delta",
            candidate_model,
            reference_model,
            samples,
            seed,
            None,
            None,
        )
    by_fold: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for (fold_name, _), errors in blocks.items():
        by_fold[fold_name].append(
            (
                sum(reference for reference, _ in errors),
                sum(candidate for _, candidate in errors),
                len(errors),
            )
        )
    random = Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        reference_total = 0.0
        candidate_total = 0.0
        observation_count = 0
        for fold_blocks in by_fold.values():
            for _ in range(len(fold_blocks)):
                sampled = fold_blocks[random.randrange(len(fold_blocks))]
                reference_total += sampled[0]
                candidate_total += sampled[1]
                observation_count += sampled[2]
        deltas.append(candidate_total / observation_count - reference_total / observation_count)
    ordered = tuple(sorted(deltas))
    return BootstrapInterval(
        "mae_delta",
        candidate_model,
        reference_model,
        samples,
        seed,
        round(_quantile(ordered, 0.025), 6),
        round(_quantile(ordered, 0.975), 6),
    )


def segment_comparisons(
    fold_results: Iterable[FoldResult],
    *,
    dataset: HistoricalFeatureDataset,
    reference_model: str,
    candidate_model: str,
    min_player_games: int = 200,
    min_games: int = 30,
) -> tuple[SegmentComparison, ...]:
    rows = {(row.player_id, row.game_id): row for row in dataset.rows}
    grouped: dict[tuple[str, str], list[tuple[str, float, float]]] = defaultdict(list)
    for fold_result in fold_results:
        reference = _observation_index(fold_result.report, reference_model)
        candidate = _observation_index(fold_result.report, candidate_model)
        for key in reference.keys() & candidate.keys():
            row = rows[key]
            tags = _segment_tags(row, fold_result.fold)
            pair = (
                row.game_id,
                reference[key].absolute_error,
                candidate[key].absolute_error,
            )
            for tag in tags:
                grouped[tag].append(pair)
    result: list[SegmentComparison] = []
    for (segment, value), records in sorted(grouped.items()):
        reference_mae = sum(record[1] for record in records) / len(records)
        candidate_mae = sum(record[2] for record in records) / len(records)
        game_count = len({record[0] for record in records})
        result.append(
            SegmentComparison(
                segment,
                value,
                candidate_model,
                reference_model,
                len(records),
                game_count,
                len(records) >= min_player_games and game_count >= min_games,
                round(reference_mae, 6),
                round(candidate_mae, 6),
                round(candidate_mae - reference_mae, 6),
            )
        )
    return tuple(result)


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


def _paired_error_blocks(
    fold_results: Iterable[FoldResult],
    *,
    reference_model: str,
    candidate_model: str,
) -> Mapping[tuple[str, str], tuple[tuple[float, float], ...]]:
    blocks: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for fold_result in fold_results:
        reference = _observation_index(fold_result.report, reference_model)
        candidate = _observation_index(fold_result.report, candidate_model)
        for key in reference.keys() & candidate.keys():
            game_id = key[1]
            blocks[(fold_result.fold.name, game_id)].append(
                (reference[key].absolute_error, candidate[key].absolute_error)
            )
    return {key: tuple(values) for key, values in blocks.items()}


def _observation_index(
    report: BacktestReport, model_name: str
) -> dict[tuple[str, str], BacktestObservation]:
    return {
        (observation.player_id, observation.game_id): observation
        for observation in report.result_for(model_name).observations
    }


def _segment_tags(
    row: HistoricalFeatureRow, fold: ChronologicalFold
) -> tuple[tuple[str, str], ...]:
    role = "starter" if row.target_started else "bench_or_low_minutes"
    if row.target_minutes is not None and row.target_minutes < 20:
        role = "bench_or_low_minutes"
    return (
        ("role", role),
        ("same_season_history", "present" if row.prior_games else "missing"),
        ("venue", "home" if row.is_home else "away"),
        (
            "back_to_back",
            "unknown" if row.is_back_to_back is None else str(row.is_back_to_back).casefold(),
        ),
        ("injury_observation", row.availability_observation.value),
        ("opponent_pace", row.opponent_pace_band),
        ("fold", fold.name),
        ("season", f"{fold.season_start}-{str(fold.season_start + 1)[-2:]}"),
    )


def _quantile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        raise ValueError("Quantile requires observations")
    position = fraction * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


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
