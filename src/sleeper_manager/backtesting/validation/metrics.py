from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from random import Random

from sleeper_manager.backtesting.models import (
    BacktestObservation,
    BacktestReport,
)
from sleeper_manager.backtesting.validation.models import (
    BootstrapInterval,
    ChronologicalFold,
    FoldResult,
    SegmentComparison,
)
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
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
