"""Estimate a Lock-In opponent's final score and the manager's chance to finish above it.

The opponent is credited with sound play: each starter locks a completed game only when it
beats the expected value of waiting. A weaker assumption would flatter the manager.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import NormalDist

from sleeper_manager.domain.projection import ProjectionDistribution

GAME_OUTCOME_COUNT = 20


@dataclass(frozen=True, slots=True)
class Finish:
    """Mean and variance of a score that is not known yet."""

    mean: float
    variance: float

    def __post_init__(self) -> None:
        if not isfinite(self.mean) or not isfinite(self.variance) or self.variance < 0:
            raise ValueError("A finish needs a finite mean and a non-negative variance")


@dataclass(frozen=True, slots=True)
class WinObjective:
    """Rank choices by the chance of finishing above ``opponent``.

    Chances within ``tie_tolerance`` of the best count as even, which leaves the choice to
    expected points.
    """

    opponent: Finish
    tie_tolerance: float = 0.005

    def __post_init__(self) -> None:
        if not isfinite(self.tie_tolerance) or not 0 <= self.tie_tolerance < 1:
            raise ValueError("Win-chance tie tolerance must be in [0, 1)")

    def chance(self, totals: Sequence[float]) -> float:
        return win_probability(totals, self.opponent)


def game_outcomes(
    distribution: ProjectionDistribution, count: int = GAME_OUTCOME_COUNT
) -> tuple[float, ...]:
    """Equally likely scores at the distribution's mid-quantiles.

    Distributions without kept observations fall back to a normal fit of their mean and variance.
    """
    if count <= 0:
        raise ValueError("Outcome count must be positive")
    fractions = tuple((index + 0.5) / count for index in range(count))
    if distribution.weighted_observations:
        ordered = sorted(distribution.weighted_observations)
        total = sum(weight for _, weight in ordered)
        return tuple(round(_quantile(ordered, total, fraction), 3) for fraction in fractions)
    if distribution.variance == 0:
        return (distribution.expected_value,)
    normal = NormalDist(distribution.expected_value, sqrt(distribution.variance))
    return tuple(round(normal.inv_cdf(fraction), 3) for fraction in fractions)


def starter_finish(upcoming: Sequence[Sequence[float]], lockable: float | None = None) -> Finish:
    """One unlocked starter's week score.

    ``upcoming`` holds equally likely outcomes for each unfinished game in schedule order;
    Sleeper counts the last one when nothing is locked. ``lockable`` is a completed game's
    score that can still be locked, or the final game's score once it is played.
    """
    if any(not outcomes for outcomes in upcoming):
        raise ValueError("Every upcoming game needs at least one outcome")
    first = second = 0.0
    for index, outcomes in enumerate(reversed(upcoming)):
        if index == 0:
            first = sum(outcomes) / len(outcomes)
            second = sum(value * value for value in outcomes) / len(outcomes)
            continue
        kept = tuple(value for value in outcomes if value >= first)
        waiting = 1 - len(kept) / len(outcomes)
        first, second = (
            sum(kept) / len(outcomes) + waiting * first,
            sum(value * value for value in kept) / len(outcomes) + waiting * second,
        )
    if lockable is not None and (not upcoming or lockable >= first):
        return Finish(lockable, 0.0)
    return Finish(first, max(second - first * first, 0.0))


def team_finish(locked_points: float, starters: Iterable[Finish]) -> Finish:
    """Locked points plus independent unlocked starters."""
    mean, variance = locked_points, 0.0
    for starter in starters:
        mean += starter.mean
        variance += starter.variance
    return Finish(mean, variance)


def win_probability(own_scores: Sequence[float], opponent: Finish) -> float:
    """Share of the manager's simulated finishes that beat a normal opponent; ties count half."""
    if not own_scores:
        raise ValueError("Win probability needs at least one simulated finish")
    if opponent.variance == 0:
        wins = sum(
            1.0 if score > opponent.mean else 0.5 if score == opponent.mean else 0.0
            for score in own_scores
        )
    else:
        normal = NormalDist(opponent.mean, sqrt(opponent.variance))
        wins = sum(normal.cdf(score) for score in own_scores)
    return round(wins / len(own_scores), 4)


def _quantile(ordered: Sequence[tuple[float, float]], total: float, fraction: float) -> float:
    threshold = fraction * total
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


__all__ = (
    "GAME_OUTCOME_COUNT",
    "Finish",
    "WinObjective",
    "game_outcomes",
    "starter_finish",
    "team_finish",
    "win_probability",
)
