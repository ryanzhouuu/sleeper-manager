from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import exp, log

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_features import HistoricalFeatureRow


class CohortRankingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CohortConfig:
    half_life_days: float = 30.0
    shrinkage_games: float = 5.0
    starter_minutes_prior: float = 24.0
    rate_prior: float = 0.5

    def __post_init__(self) -> None:
        if self.half_life_days <= 0 or self.shrinkage_games < 0:
            raise CohortRankingError("Cohort windows and shrinkage must be non-negative")
        if self.starter_minutes_prior < 0 or self.rate_prior < 0:
            raise CohortRankingError("Cohort priors must be non-negative")


@dataclass(frozen=True, slots=True)
class RankedPlayer:
    player_id: str
    rank: int
    baseline_score: float
    prior_games: int
    cohort: str


def rank_players_as_of(
    rows: Iterable[HistoricalFeatureRow],
    as_of: datetime,
    *,
    player_ids: Iterable[str] = (),
    config: CohortConfig | None = None,
) -> dict[str, int]:
    """Rank players using only observations strictly before ``as_of``."""

    if as_of.tzinfo is None:
        raise CohortRankingError("Cohort ranking timestamp must be timezone-aware")
    ranked = ranked_players_as_of(rows, as_of, player_ids=player_ids, config=config)
    return {player.player_id: player.rank for player in ranked}


def ranked_players_as_of(
    rows: Iterable[HistoricalFeatureRow],
    as_of: datetime,
    *,
    player_ids: Iterable[str] = (),
    config: CohortConfig | None = None,
) -> tuple[RankedPlayer, ...]:
    config = config or CohortConfig()
    records = tuple(row for row in rows if row.game_start < as_of)
    by_player: dict[str, list[HistoricalFeatureRow]] = {}
    for row in records:
        by_player.setdefault(row.player_id, []).append(row)
    all_players = set(player_ids) | set(by_player)
    scored = tuple(
        (
            player_id,
            _baseline_score(by_player.get(player_id, ()), as_of, config),
            len(by_player.get(player_id, ())),
        )
        for player_id in all_players
    )
    ordered = tuple(sorted(scored, key=lambda item: (-item[1], item[0])))
    return tuple(
        RankedPlayer(
            player_id=player_id,
            rank=rank,
            baseline_score=round(score, 6),
            prior_games=prior_games,
            cohort=cohort_for_rank(rank),
        )
        for rank, (player_id, score, prior_games) in enumerate(ordered, start=1)
    )


class IndependentCohortRanker:
    """Caches one model-independent ranking map per decision timestamp."""

    def __init__(self, config: CohortConfig | None = None) -> None:
        self.config = config or CohortConfig()
        self._cache: dict[datetime, dict[str, int]] = {}

    def rank_players_as_of(
        self,
        rows: Iterable[HistoricalFeatureRow],
        as_of: datetime,
        *,
        player_ids: Iterable[str] = (),
    ) -> dict[str, int]:
        if not player_ids and as_of in self._cache:
            return dict(self._cache[as_of])
        ranks = rank_players_as_of(rows, as_of, player_ids=player_ids, config=self.config)
        if not player_ids:
            self._cache[as_of] = dict(ranks)
        return ranks


def cohort_for_rank(rank: int) -> str:
    if rank <= 0:
        raise CohortRankingError("Player ranks must be positive")
    if rank <= 108:
        return "top_108"
    if rank <= 180:
        return "ranks_109_180"
    return "below_180"


def cohort_counts(ranks: Mapping[str, int]) -> dict[str, int]:
    counts = {"top_108": 0, "ranks_109_180": 0, "below_180": 0}
    for rank in ranks.values():
        counts[cohort_for_rank(rank)] += 1
    return counts


def _baseline_score(
    rows: Sequence[HistoricalFeatureRow], as_of: datetime, config: CohortConfig
) -> float:
    if not rows:
        return 0.0
    weighted_minutes: list[tuple[float, float]] = []
    weighted_rates: list[tuple[float, float]] = []
    for row in rows:
        if row.game_start >= as_of or not row.target_did_play or not row.target_minutes:
            continue
        weight = exp(
            -log(2)
            * max((as_of - row.game_start).total_seconds(), 0)
            / 86400
            / config.half_life_days
        )
        weighted_minutes.append((row.target_minutes, weight))
        weighted_rates.append(
            (row.target_box_score.points / row.target_minutes, weight * row.target_minutes)
        )
    if not weighted_minutes:
        return 0.0
    minutes = _weighted_mean(weighted_minutes)
    rate = _weighted_mean(weighted_rates)
    participation = _participation(rows)
    status = _status_probability(rows[-1].availability_status)
    availability = (2 * participation + status) / 3
    effective_games = sum(weight for _, weight in weighted_minutes)
    shrinkage = effective_games / (effective_games + config.shrinkage_games)
    minutes = config.starter_minutes_prior + shrinkage * (minutes - config.starter_minutes_prior)
    rate = config.rate_prior + shrinkage * (rate - config.rate_prior)
    return availability * minutes * rate


def _weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    records = tuple(values)
    total = sum(weight for _, weight in records)
    return sum(value * weight for value, weight in records) / total if total else 0.0


def _participation(rows: Sequence[HistoricalFeatureRow]) -> float:
    return sum(row.target_did_play for row in rows) / len(rows) if rows else 0.0


def _status_probability(status: AvailabilityStatus) -> float:
    return {
        AvailabilityStatus.AVAILABLE: 0.98,
        AvailabilityStatus.PROBABLE: 0.92,
        AvailabilityStatus.QUESTIONABLE: 0.68,
        AvailabilityStatus.DOUBTFUL: 0.35,
        AvailabilityStatus.OUT: 0.02,
        AvailabilityStatus.UNKNOWN: 0.75,
    }[status]


__all__ = (
    "CohortConfig",
    "CohortRankingError",
    "IndependentCohortRanker",
    "RankedPlayer",
    "cohort_counts",
    "cohort_for_rank",
    "rank_players_as_of",
    "ranked_players_as_of",
)
