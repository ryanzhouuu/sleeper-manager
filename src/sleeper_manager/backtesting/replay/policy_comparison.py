"""Strict admission and metrics for paired historical policy comparisons.

This module owns comparison-time checks that a score-difference helper cannot prove:
input compatibility, oracle identity, invariant success, and complete-pair accounting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite

from sleeper_manager.backtesting.replay.validation import (
    BootstrapInterval,
    ReplayValidationError,
    bootstrap_mean_interval,
)


@dataclass(frozen=True, slots=True)
class ComparablePolicyScore:
    """Bind one policy score to the shared evidence and oracle used for comparison."""

    team_week_key: str
    score: float
    oracle_score: float
    compatibility_fingerprint: str
    invariant_results: tuple[tuple[str, bool], ...]
    complete: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PairedRegretDelta:
    """Report admitted regret and capture changes with visible missing-pair accounting."""

    common_team_week_count: int
    candidate_unpaired_count: int
    baseline_unpaired_count: int
    candidate_incomplete_count: int
    baseline_incomplete_count: int
    nonpositive_oracle_pair_count: int
    mean_candidate_minus_baseline_regret: float | None
    regret_interval: BootstrapInterval | None
    mean_improvement: float | None
    improvement_interval: BootstrapInterval | None
    mean_capture_change: float | None
    capture_change_interval: BootstrapInterval | None


@dataclass(frozen=True, slots=True)
class ConcentrationResult:
    """Describe signed contribution shares when aggregate improvement is positive."""

    net_improvement: float
    shares: tuple[tuple[str, float], ...] | None


def paired_policy_regret_delta(
    candidate: Iterable[ComparablePolicyScore],
    baseline: Iterable[ComparablePolicyScore],
    *,
    samples: int = 1000,
    seed: int = 0,
    oracle_tolerance: float = 1e-6,
) -> PairedRegretDelta:
    """Admit compatible complete pairs and calculate canonical candidate-minus-baseline regret."""

    if oracle_tolerance < 0:
        raise ReplayValidationError("Oracle tolerance cannot be negative")
    candidate_records = tuple(candidate)
    baseline_records = tuple(baseline)
    candidate_by_key = _unique_complete_scores(candidate_records, label="candidate")
    baseline_by_key = _unique_complete_scores(baseline_records, label="baseline")
    keys = tuple(sorted(candidate_by_key.keys() & baseline_by_key.keys()))
    regret_deltas: list[float] = []
    capture_deltas: list[float] = []
    nonpositive_oracle_pairs = 0
    for key in keys:
        candidate_score = candidate_by_key[key]
        baseline_score = baseline_by_key[key]
        _validate_pair(candidate_score, baseline_score, oracle_tolerance=oracle_tolerance)
        regret_deltas.append(baseline_score.score - candidate_score.score)
        if candidate_score.oracle_score > 0:
            capture_deltas.append(
                (candidate_score.score - baseline_score.score) / candidate_score.oracle_score
            )
        else:
            nonpositive_oracle_pairs += 1
    regret_values = tuple(regret_deltas)
    improvement_values = tuple(-value for value in regret_values)
    capture_values = tuple(capture_deltas)
    return PairedRegretDelta(
        common_team_week_count=len(keys),
        candidate_unpaired_count=len(candidate_by_key.keys() - baseline_by_key.keys()),
        baseline_unpaired_count=len(baseline_by_key.keys() - candidate_by_key.keys()),
        candidate_incomplete_count=sum(not record.complete for record in candidate_records),
        baseline_incomplete_count=sum(not record.complete for record in baseline_records),
        nonpositive_oracle_pair_count=nonpositive_oracle_pairs,
        mean_candidate_minus_baseline_regret=_mean(regret_values),
        regret_interval=_interval(regret_values, samples=samples, seed=seed),
        mean_improvement=_mean(improvement_values),
        improvement_interval=_interval(improvement_values, samples=samples, seed=seed),
        mean_capture_change=_mean(capture_values),
        capture_change_interval=_interval(capture_values, samples=samples, seed=seed),
    )


def interval_direction(interval: BootstrapInterval | None) -> str:
    """Classify a signed interval without treating a zero crossing as non-regression."""

    if interval is None:
        return "insufficient_evidence"
    if interval.lower > 0:
        return "improved"
    if interval.upper < 0:
        return "regressed"
    return "inconclusive"


def concentration_shares(improvement_by_group: Mapping[str, float]) -> ConcentrationResult:
    """Return signed group shares, or undefined shares when net improvement is not positive."""

    if not all(isfinite(value) for value in improvement_by_group.values()):
        raise ReplayValidationError("Concentration inputs must be finite")
    net = sum(improvement_by_group.values())
    if net <= 0:
        return ConcentrationResult(net_improvement=round(net, 6), shares=None)
    shares = tuple(
        (group, round(value / net, 6)) for group, value in sorted(improvement_by_group.items())
    )
    return ConcentrationResult(net_improvement=round(net, 6), shares=shares)


def _unique_complete_scores(
    records: tuple[ComparablePolicyScore, ...], *, label: str
) -> dict[str, ComparablePolicyScore]:
    """Reject duplicate logical rows before filtering incomplete attempts."""

    by_key: dict[str, ComparablePolicyScore] = {}
    for record in records:
        if record.team_week_key in by_key:
            raise ReplayValidationError(f"Duplicate {label} team-week: {record.team_week_key}")
        by_key[record.team_week_key] = record
    return {key: record for key, record in by_key.items() if record.complete}


def _validate_pair(
    candidate: ComparablePolicyScore,
    baseline: ComparablePolicyScore,
    *,
    oracle_tolerance: float,
) -> None:
    """Fail closed when a nominal pair does not share admissible evidence and constraints."""

    values = (candidate.score, candidate.oracle_score, baseline.score, baseline.oracle_score)
    if not all(isfinite(value) for value in values):
        raise ReplayValidationError(f"Non-finite paired score: {candidate.team_week_key}")
    if not candidate.invariant_results or not baseline.invariant_results:
        raise ReplayValidationError(f"Missing paired invariants: {candidate.team_week_key}")
    if (
        candidate.score > candidate.oracle_score + oracle_tolerance
        or baseline.score > baseline.oracle_score + oracle_tolerance
    ):
        raise ReplayValidationError(
            f"Policy score exceeds paired oracle: {candidate.team_week_key}"
        )
    if candidate.compatibility_fingerprint != baseline.compatibility_fingerprint:
        raise ReplayValidationError(f"Incompatible paired inputs: {candidate.team_week_key}")
    if abs(candidate.oracle_score - baseline.oracle_score) > oracle_tolerance:
        raise ReplayValidationError(f"Mismatched paired oracle: {candidate.team_week_key}")
    if any(not passed for _, passed in (*candidate.invariant_results, *baseline.invariant_results)):
        raise ReplayValidationError(f"Failed paired invariant: {candidate.team_week_key}")


def _mean(values: tuple[float, ...]) -> float | None:
    """Return a rounded arithmetic mean while preserving empty evidence as missing."""

    return round(sum(values) / len(values), 6) if values else None


def _interval(values: tuple[float, ...], *, samples: int, seed: int) -> BootstrapInterval | None:
    """Bootstrap a whole-team-week mean only when at least one admitted row exists."""

    return bootstrap_mean_interval(values, samples=samples, seed=seed) if values else None


__all__ = (
    "ComparablePolicyScore",
    "ConcentrationResult",
    "PairedRegretDelta",
    "concentration_shares",
    "interval_direction",
    "paired_policy_regret_delta",
)
