"""Dependency-free entropy projection onto core-stat mean tolerance intervals.

Coordinate minimization solves the exponential-tilt dual. Finite support failures
and the iteration budget return explicit failures, never invented stat outcomes.
"""

from dataclasses import dataclass
from math import exp, fsum, isfinite, log
from statistics import pstdev

from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError

NUMERICAL_TOLERANCE = 1e-8


@dataclass(frozen=True, slots=True)
class JointWeightFit:
    """A converged set of weights or a reason to use the independent internal pool."""

    weights: tuple[float, ...] = ()
    failure: str | None = None
    iterations: int = 0


def weight_support(weights: tuple[float, ...], config: HybridProjectionConfig) -> str | None:
    """Check weight concentration, not statistical independence or calibration."""
    if not weights or any(not isfinite(w) or w < 0 for w in weights):
        return "invalid_distribution_weights"
    if abs(fsum(weights) - 1) > 1e-7:
        return "unnormalized_distribution_weights"
    if 1 / fsum(w * w for w in weights) + 1e-7 < config.minimum_effective_sample:
        return "insufficient_effective_sample"
    if max(weights) > config.maximum_game_weight + 1e-8:
        return "concentrated_game_weight"
    return None


def fit_joint_weights(
    vectors: tuple[tuple[float, ...], ...],
    base_weights: tuple[float, ...],
    center: tuple[float, ...],
    config: HybridProjectionConfig,
) -> JointWeightFit:
    """Minimize KL departure from positive base weights subject to tolerated means."""
    if not vectors or len(vectors) != len(base_weights) or len(center) != 7:
        raise HybridProjectionError("invalid_fit_dimensions")
    if any(len(row) != 7 for row in vectors) or any(
        not isfinite(x) or x < 0 for row in (*vectors, center) for x in row
    ):
        raise HybridProjectionError("invalid_fit_stats")
    if any(not isfinite(w) or w <= 0 for w in base_weights):
        raise HybridProjectionError("invalid_fit_base_weights")
    total = fsum(base_weights)
    base = tuple(w / total for w in base_weights)
    tolerances = tuple(
        max(config.relative_mean_tolerance * value, floor)
        for value, floor in zip(center, config.mean_tolerance_floors, strict=True)
    )
    columns = tuple(tuple(row[k] for row in vectors) for k in range(7))
    if any(
        max(column) < goal - tol or min(column) > goal + tol
        for column, goal, tol in zip(columns, center, tolerances, strict=True)
    ):
        return JointWeightFit(failure="external_center_outside_support")
    scales = tuple(
        max(pstdev(column), floor)
        for column, floor in zip(columns, config.profile_scale_floors, strict=True)
    )
    columns = tuple(
        tuple(x / scale for x in column) for column, scale in zip(columns, scales, strict=True)
    )
    lower = tuple(
        (goal - tol) / scale for goal, tol, scale in zip(center, tolerances, scales, strict=True)
    )
    upper = tuple(
        (goal + tol) / scale for goal, tol, scale in zip(center, tolerances, scales, strict=True)
    )
    logits = tuple(log(w) for w in base)
    coefficients = [0.0] * 7
    weights = base
    for iteration in range(config.fit_iterations + 1):
        means = tuple(_mean(column, weights) for column in columns)
        errors = tuple(
            abs(means[k] - lower[k])
            if coefficients[k] > NUMERICAL_TOLERANCE
            else abs(means[k] - upper[k])
            if coefficients[k] < -NUMERICAL_TOLERANCE
            else max(lower[k] - means[k], means[k] - upper[k], 0.0)
            for k in range(7)
        )
        if max(errors) <= NUMERICAL_TOLERANCE:
            return JointWeightFit(weights, iterations=iteration)
        if iteration == config.fit_iterations:
            break
        k = iteration % 7
        column = columns[k]
        without = tuple(
            value - coefficients[k] * x for value, x in zip(logits, column, strict=True)
        )
        original_mean = _mean(column, _probabilities(without))
        if lower[k] <= original_mean <= upper[k]:
            coefficient = 0.0
        else:
            target = lower[k] if original_mean < lower[k] else upper[k]
            coefficient = _solve_coordinate(without, column, target, original_mean < target)
        coefficients[k] = coefficient
        logits = tuple(value + coefficient * x for value, x in zip(without, column, strict=True))
        weights = _probabilities(logits)
    return JointWeightFit(failure="external_fit_nonconverged", iterations=config.fit_iterations)


def _probabilities(logits: tuple[float, ...]) -> tuple[float, ...]:
    """Stable softmax permits negligible outcomes to underflow without overflow."""
    peak = max(logits)
    values = tuple(exp(value - peak) for value in logits)
    total = fsum(values)
    return tuple(value / total for value in values)


def _mean(column: tuple[float, ...], weights: tuple[float, ...]) -> float:
    return fsum(value * weight for value, weight in zip(column, weights, strict=True))


def _solve_coordinate(
    logits: tuple[float, ...],
    column: tuple[float, ...],
    target: float,
    increase: bool,
) -> float:
    """Bracket a monotone moment root; bounded coefficients expose infeasible joint fits."""
    low, high = (0.0, 64.0) if increase else (-64.0, 0.0)
    for _ in range(45):
        mid = (low + high) / 2
        tilted = tuple(value + mid * x for value, x in zip(logits, column, strict=True))
        mean = _mean(column, _probabilities(tilted))
        if mean < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2
