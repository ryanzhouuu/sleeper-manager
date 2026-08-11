from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from math import erf, isfinite, sqrt


class ProjectionCompatibilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectionReason:
    code: str
    message: str
    applied: bool = True
    adjustment: float | None = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ProjectionCompatibilityError("Projection reasons require a code and message")
        if self.adjustment is not None and not isfinite(self.adjustment):
            raise ProjectionCompatibilityError("Projection reason adjustment must be finite")


@dataclass(frozen=True, slots=True)
class ProjectionDistribution:
    expected_value: float
    median: float
    percentiles: tuple[tuple[int, float], ...]
    lower_bound: float
    upper_bound: float
    variance: float
    weighted_observations: tuple[tuple[float, float], ...] = ()
    exceedance_score: float | None = None
    probability_exceeding_score: float | None = None

    def __post_init__(self) -> None:
        numeric_values = (
            self.expected_value,
            self.median,
            self.lower_bound,
            self.upper_bound,
            self.variance,
        )
        if not all(isfinite(value) for value in numeric_values):
            raise ProjectionCompatibilityError("Projection distribution values must be finite")
        if self.variance < 0:
            raise ProjectionCompatibilityError("Projection variance must be non-negative")
        if self.lower_bound > self.upper_bound:
            raise ProjectionCompatibilityError("Projection range is inverted")
        if not self.lower_bound <= self.median <= self.upper_bound:
            raise ProjectionCompatibilityError("Projection median must be within its range")
        previous_percentile = -1
        for percentile, value in self.percentiles:
            if percentile <= previous_percentile or not 0 <= percentile <= 100:
                raise ProjectionCompatibilityError("Projection percentiles must be ordered 0-100")
            if not isfinite(value) or not self.lower_bound <= value <= self.upper_bound:
                raise ProjectionCompatibilityError("Projection percentile is outside its range")
            previous_percentile = percentile
        if (self.exceedance_score is None) != (self.probability_exceeding_score is None):
            raise ProjectionCompatibilityError(
                "Exceedance score and probability must be supplied together"
            )
        if self.exceedance_score is not None and not isfinite(self.exceedance_score):
            raise ProjectionCompatibilityError("Exceedance score must be finite")
        if self.probability_exceeding_score is not None and not (
            0 <= self.probability_exceeding_score <= 1
        ):
            raise ProjectionCompatibilityError("Exceedance probability must be between 0 and 1")
        total_weight = 0.0
        for value, weight in self.weighted_observations:
            if not isfinite(value) or not isfinite(weight) or weight <= 0:
                raise ProjectionCompatibilityError(
                    "Weighted observations must be finite and positive"
                )
            total_weight += weight
        if self.weighted_observations and total_weight <= 0:
            raise ProjectionCompatibilityError("Projection observations require positive weight")

    @property
    def range(self) -> tuple[float, float]:
        return self.lower_bound, self.upper_bound

    @classmethod
    def from_weighted_observations(
        cls,
        observations: Iterable[tuple[float, float]],
        *,
        percentiles: tuple[int, ...] = (10, 25, 50, 75, 90),
    ) -> ProjectionDistribution:
        normalized = tuple((float(value), float(weight)) for value, weight in observations)
        if not normalized:
            raise ProjectionCompatibilityError("Projection requires at least one observation")
        if not percentiles:
            raise ProjectionCompatibilityError("Projection requires at least one percentile")
        if any(percentile < 0 or percentile > 100 for percentile in percentiles):
            raise ProjectionCompatibilityError("Projection percentiles must be ordered 0-100")
        if tuple(sorted(set(percentiles))) != percentiles:
            raise ProjectionCompatibilityError("Projection percentiles must be unique and ordered")
        total_weight = sum(weight for _, weight in normalized)
        if total_weight <= 0 or not isfinite(total_weight):
            raise ProjectionCompatibilityError("Projection observations require positive weight")
        expected = sum(value * weight for value, weight in normalized) / total_weight
        variance = (
            sum(weight * (value - expected) ** 2 for value, weight in normalized) / total_weight
        )
        ordered = tuple(sorted(normalized))
        lower_bound = ordered[0][0]
        upper_bound = ordered[-1][0]
        values = tuple(
            (percentile, _weighted_quantile(ordered, total_weight, percentile / 100))
            for percentile in percentiles
        )
        median = (
            next(value for percentile, value in values if percentile == 50)
            if 50 in percentiles
            else _weighted_quantile(ordered, total_weight, 0.5)
        )
        return cls(
            expected_value=round(expected, 6),
            median=round(median, 6),
            percentiles=tuple((percentile, round(value, 6)) for percentile, value in values),
            lower_bound=round(lower_bound, 6),
            upper_bound=round(upper_bound, 6),
            variance=round(variance, 6),
            weighted_observations=normalized,
        )

    def probability_of_exceeding(self, score: float) -> float:
        if not isfinite(score):
            raise ValueError("Exceedance score must be finite")
        if self.weighted_observations:
            total_weight = sum(weight for _, weight in self.weighted_observations)
            probability = (
                sum(weight for value, weight in self.weighted_observations if value > score)
                / total_weight
            )
            return round(probability, 6)
        if self.variance == 0:
            return 1.0 if self.expected_value > score else 0.0
        standard_deviation = sqrt(self.variance)
        z_score = (score - self.expected_value) / standard_deviation
        return round(0.5 * (1 - erf(z_score / sqrt(2))), 6)

    def for_exceedance_score(self, score: float) -> ProjectionDistribution:
        return replace(
            self,
            exceedance_score=float(score),
            probability_exceeding_score=self.probability_of_exceeding(score),
        )


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    player_id: str
    game_id: str
    available_as_of: datetime
    model_version: str
    input_version: str
    scoring_policy_version: str
    distribution: ProjectionDistribution
    reasons: tuple[ProjectionReason, ...]


def _weighted_quantile(
    observations: tuple[tuple[float, float], ...], total_weight: float, fraction: float
) -> float:
    threshold = fraction * total_weight
    cumulative = 0.0
    for value, weight in observations:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return observations[-1][0]
