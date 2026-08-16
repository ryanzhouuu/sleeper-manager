from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from statistics import median

from sleeper_manager.backtesting.replay_models import TeamWeekComparison


class ReplayValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    lower: float
    upper: float
    confidence: float = 0.95


@dataclass(frozen=True, slots=True)
class ReplayMetricSummary:
    team_week_count: int
    mean_regret: float | None
    median_regret: float | None
    regret_percentiles: tuple[tuple[int, float], ...]
    mean_score_capture: float | None
    aggregate_score_capture: float | None
    regret_per_starter_slot: float | None
    mean_regret_interval: BootstrapInterval | None
    excluded_team_weeks: int


@dataclass(frozen=True, slots=True)
class PolicyScore:
    team_week_key: str
    score: float
    oracle_score: float
    complete: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PairedPolicyDelta:
    common_team_week_count: int
    mean_candidate_minus_baseline: float | None
    interval: BootstrapInterval | None


def summarize_team_weeks(
    comparisons: Iterable[TeamWeekComparison],
    *,
    starter_slot_count: int = 9,
    bootstrap_samples: int = 1000,
    seed: int = 0,
) -> ReplayMetricSummary:
    if starter_slot_count <= 0:
        raise ReplayValidationError("Starter slot count must be positive")
    records = tuple(comparisons)
    regrets = tuple(record.lock_in_regret for record in records)
    captures = tuple(record.score_capture for record in records if record.score_capture is not None)
    if not regrets:
        return ReplayMetricSummary(0, None, None, (), None, None, None, None, 0)
    oracle_total = sum(record.oracle_team_score for record in records)
    model_total = sum(record.model_policy_team_score for record in records)
    return ReplayMetricSummary(
        team_week_count=len(records),
        mean_regret=round(sum(regrets) / len(regrets), 6),
        median_regret=round(median(regrets), 6),
        regret_percentiles=tuple(
            (percentile, round(_quantile(regrets, percentile / 100), 6))
            for percentile in (10, 25, 50, 75, 90)
        ),
        mean_score_capture=(round(sum(captures) / len(captures), 6) if captures else None),
        aggregate_score_capture=(round(model_total / oracle_total, 6) if oracle_total else None),
        regret_per_starter_slot=round(sum(regrets) / len(regrets) / starter_slot_count, 6),
        mean_regret_interval=bootstrap_mean_interval(regrets, samples=bootstrap_samples, seed=seed),
        excluded_team_weeks=0,
    )


def summarize_with_exclusions(
    comparisons: Iterable[TeamWeekComparison],
    exclusions: Iterable[str],
    *,
    starter_slot_count: int = 9,
    bootstrap_samples: int = 1000,
    seed: int = 0,
) -> ReplayMetricSummary:
    summary = summarize_team_weeks(
        comparisons,
        starter_slot_count=starter_slot_count,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )
    return replace(summary, excluded_team_weeks=len(tuple(exclusions)))


def paired_policy_delta(
    candidate: Iterable[PolicyScore],
    baseline: Iterable[PolicyScore],
    *,
    samples: int = 1000,
    seed: int = 0,
) -> PairedPolicyDelta:
    candidate_by_key = {record.team_week_key: record for record in candidate if record.complete}
    baseline_by_key = {record.team_week_key: record for record in baseline if record.complete}
    keys = tuple(sorted(candidate_by_key.keys() & baseline_by_key.keys()))
    deltas = tuple(candidate_by_key[key].score - baseline_by_key[key].score for key in keys)
    return PairedPolicyDelta(
        common_team_week_count=len(deltas),
        mean_candidate_minus_baseline=(round(sum(deltas) / len(deltas), 6) if deltas else None),
        interval=bootstrap_mean_interval(deltas, samples=samples, seed=seed) if deltas else None,
    )


def bootstrap_mean_interval(
    values: Sequence[float], *, samples: int = 1000, seed: int = 0, confidence: float = 0.95
) -> BootstrapInterval | None:
    if not values:
        return None
    if samples <= 0 or not 0 < confidence < 1:
        raise ReplayValidationError("Bootstrap samples and confidence must be valid")
    randomizer = random.Random(seed)
    bootstrap_means = tuple(
        sum(randomizer.choice(values) for _ in values) / len(values) for _ in range(samples)
    )
    tail = (1 - confidence) / 2
    return BootstrapInterval(
        lower=round(_quantile(bootstrap_means, tail), 6),
        upper=round(_quantile(bootstrap_means, 1 - tail), 6),
        confidence=confidence,
    )


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ReplayValidationError("Cannot calculate a quantile of an empty sequence")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


__all__ = (
    "BootstrapInterval",
    "PairedPolicyDelta",
    "PolicyScore",
    "ReplayMetricSummary",
    "ReplayValidationError",
    "bootstrap_mean_interval",
    "paired_policy_delta",
    "summarize_team_weeks",
    "summarize_with_exclusions",
)
