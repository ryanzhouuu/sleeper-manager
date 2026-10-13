"""Lossless participation sufficient statistics for bounded runtime history reads."""

from collections import defaultdict
from dataclasses import dataclass

from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_types import ParticipationOpportunity


@dataclass(frozen=True, slots=True)
class ParticipationCounts:
    """Each bucket stores played/total counts; untagged opportunities stay in the denominator."""

    played: int = 0
    total: int = 0
    buckets: tuple[tuple[str, int, int], ...] = ()

    def __post_init__(self) -> None:
        """Reject contradictory or duplicate counts at the persisted-data boundary."""
        rows = (("all", self.played, self.total), *self.buckets)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for _, played, total in rows
            for value in (played, total)
        ):
            raise HybridProjectionError("invalid_participation_counts")
        if any(played > total for _, played, total in rows):
            raise HybridProjectionError("invalid_participation_counts")
        if len({bucket for bucket, _, _ in self.buckets}) != len(self.buckets):
            raise HybridProjectionError("duplicate_participation_bucket")
        if sum(total for _, _, total in self.buckets) > self.total:
            raise HybridProjectionError("invalid_participation_counts")
        if (
            sum(played for _, played, _ in self.buckets) > self.played
            or sum(total - played for _, played, total in self.buckets) > self.total - self.played
        ):
            raise HybridProjectionError("invalid_participation_counts")

    def bucket(self, name: str | None) -> tuple[int, int]:
        return next(
            ((played, total) for bucket, played, total in self.buckets if bucket == name), (0, 0)
        )


def merge_participation_counts(rows: tuple[ParticipationCounts, ...]) -> ParticipationCounts:
    """Add disjoint census aggregates without changing any opportunity weights."""
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        for bucket, played, total in row.buckets:
            buckets[bucket][0] += played
            buckets[bucket][1] += total
    return ParticipationCounts(
        sum(row.played for row in rows),
        sum(row.total for row in rows),
        tuple((bucket, values[0], values[1]) for bucket, values in sorted(buckets.items())),
    )


def census_participation_counts(
    opportunities: tuple[ParticipationOpportunity, ...],
) -> dict[str, ParticipationCounts]:
    """Aggregate already cutoff-filtered opportunities, preserving observation/status groups."""
    rows: dict[str, list[ParticipationCounts]] = defaultdict(list)
    for row in opportunities:
        rows[row.player_id].append(
            ParticipationCounts(
                int(row.did_play),
                1,
                ((row.availability.bucket, int(row.did_play), 1),) if row.availability else (),
            )
        )
    return {
        player: merge_participation_counts(tuple(values)) for player, values in sorted(rows.items())
    }
