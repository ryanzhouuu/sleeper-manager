from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp, log

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
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

    @property
    def version(self) -> str:
        payload = {
            "half_life_days": self.half_life_days,
            "shrinkage_games": self.shrinkage_games,
            "starter_minutes_prior": self.starter_minutes_prior,
            "rate_prior": self.rate_prior,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
        return f"cohort-config-v1-{digest}"


@dataclass(frozen=True, slots=True)
class RankedPlayer:
    player_id: str
    rank: int
    baseline_score: float
    prior_games: int
    cohort: str
    top_180: bool


def rank_players_as_of(
    rows: Iterable[HistoricalFeatureRow],
    as_of: datetime,
    *,
    player_ids: Iterable[str] = (),
    config: CohortConfig | None = None,
    scoring_policy: ScoringPolicy,
) -> dict[str, int]:
    """Rank players using only observations strictly before ``as_of``."""

    if as_of.tzinfo is None:
        raise CohortRankingError("Cohort ranking timestamp must be timezone-aware")
    ranked = ranked_players_as_of(
        rows, as_of, player_ids=player_ids, config=config, scoring_policy=scoring_policy
    )
    return {player.player_id: player.rank for player in ranked}


def ranked_players_as_of(
    rows: Iterable[HistoricalFeatureRow],
    as_of: datetime,
    *,
    player_ids: Iterable[str] = (),
    config: CohortConfig | None = None,
    scoring_policy: ScoringPolicy,
) -> tuple[RankedPlayer, ...]:
    config = config or CohortConfig()
    by_player: dict[str, list[HistoricalFeatureRow]] = {}
    for row in rows:
        if row.game_start < as_of:
            by_player.setdefault(row.player_id, []).append(row)
    return _rank_from_groups(
        by_player,
        as_of,
        player_ids=frozenset(player_ids),
        config=config,
        scoring_policy=scoring_policy,
    )


class IndependentCohortRanker:
    """Incrementally ranks players from a growing prior-only row prefix.

    The index is keyed by dataset version, scoring policy, and cohort configuration: any change
    to that key discards the accumulated state rather than silently reusing an incompatible
    index. When ``rows`` is the same stable sequence object across calls (the normal
    chronological-backtest usage), only the rows newly eligible since the previous call are
    grouped; a different sequence object triggers a full, correct rebuild.
    """

    def __init__(self, config: CohortConfig | None = None) -> None:
        self.config = config or CohortConfig()
        self._version_key: tuple[str, str, str] | None = None
        self._rows: Sequence[HistoricalFeatureRow] | None = None
        self._game_starts: list[datetime] = []
        self._by_player: dict[str, list[HistoricalFeatureRow]] = {}
        self._committed = 0
        self._cache: dict[datetime, dict[str, int]] = {}

    def rank_players_as_of(
        self,
        rows: Sequence[HistoricalFeatureRow],
        as_of: datetime,
        *,
        scoring_policy: ScoringPolicy,
        dataset_version: str = "",
        player_ids: Iterable[str] = (),
    ) -> dict[str, int]:
        if as_of.tzinfo is None:
            raise CohortRankingError("Cohort ranking timestamp must be timezone-aware")
        version_key = (dataset_version, scoring_policy.version, self.config.version)
        if version_key != self._version_key or rows is not self._rows:
            self._rebuild(rows, version_key)
        eligible_count = bisect_left(self._game_starts, as_of)
        if eligible_count < self._committed:
            raise CohortRankingError("Cohort ranking index cannot move backward")
        for row in self._rows[self._committed : eligible_count]:  # type: ignore[index]
            self._by_player.setdefault(row.player_id, []).append(row)
        self._committed = eligible_count
        player_id_set = frozenset(player_ids)
        if not player_id_set and as_of in self._cache:
            return dict(self._cache[as_of])
        ranked = _rank_from_groups(
            self._by_player,
            as_of,
            player_ids=player_id_set,
            config=self.config,
            scoring_policy=scoring_policy,
        )
        ranks = {player.player_id: player.rank for player in ranked}
        if not player_id_set:
            self._cache[as_of] = dict(ranks)
        return ranks

    def _rebuild(
        self, rows: Sequence[HistoricalFeatureRow], version_key: tuple[str, str, str]
    ) -> None:
        game_starts = [row.game_start for row in rows]
        for earlier, later in zip(game_starts, game_starts[1:], strict=False):
            if earlier > later:
                raise CohortRankingError("Cohort ranking rows must be chronologically ordered")
        self._version_key = version_key
        self._rows = rows
        self._game_starts = game_starts
        self._by_player = {}
        self._committed = 0
        self._cache = {}


def _rank_from_groups(
    by_player: Mapping[str, Sequence[HistoricalFeatureRow]],
    as_of: datetime,
    *,
    player_ids: frozenset[str],
    config: CohortConfig,
    scoring_policy: ScoringPolicy,
) -> tuple[RankedPlayer, ...]:
    current_season = _season_start_year(as_of)
    current_season_players = {
        player_id
        for player_id, player_rows in by_player.items()
        if any(_season_start_year(row.game_start) == current_season for row in player_rows)
    }
    all_players = player_ids | current_season_players
    scored = tuple(
        (
            player_id,
            _baseline_score(by_player.get(player_id, ()), as_of, config, scoring_policy),
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
            top_180=rank <= 180,
        )
        for rank, (player_id, score, prior_games) in enumerate(ordered, start=1)
    )


def cohort_for_rank(rank: int) -> str:
    if rank <= 0:
        raise CohortRankingError("Player ranks must be positive")
    if rank <= 108:
        return "top_108"
    if rank <= 180:
        return "ranks_109_180"
    return "below_180"


def cohort_counts(ranks: Mapping[str, int]) -> dict[str, int]:
    counts = {"top_108": 0, "ranks_109_180": 0, "below_180": 0, "top_180": 0}
    for rank in ranks.values():
        counts[cohort_for_rank(rank)] += 1
        if rank <= 180:
            counts["top_180"] += 1
    return counts


def _baseline_score(
    rows: Sequence[HistoricalFeatureRow],
    as_of: datetime,
    config: CohortConfig,
    scoring_policy: ScoringPolicy,
) -> float:
    if not rows:
        return 0.0
    ordered = tuple(sorted(rows, key=lambda row: row.game_start))
    current_season = _season_start_year(as_of)
    current_rows = tuple(
        row for row in ordered if _season_start_year(row.game_start) == current_season
    )
    prior_season_rows = tuple(
        row for row in ordered if _season_start_year(row.game_start) == current_season - 1
    )
    current_minutes, current_rate, current_effective = _weighted_playing_stats(
        current_rows, as_of, config, scoring_policy
    )
    if prior_season_rows:
        prior_anchor = prior_season_rows[-1].game_start + timedelta(microseconds=1)
        fallback_minutes, fallback_rate, _ = _weighted_playing_stats(
            prior_season_rows, prior_anchor, config, scoring_policy
        )
    else:
        fallback_minutes, fallback_rate = config.starter_minutes_prior, config.rate_prior
    shrinkage = current_effective / (current_effective + config.shrinkage_games)
    minutes = fallback_minutes + shrinkage * (current_minutes - fallback_minutes)
    rate = fallback_rate + shrinkage * (current_rate - fallback_rate)
    participation = _participation(ordered)
    status = _status_probability(ordered[-1].availability_status)
    availability = (2 * participation + status) / 3
    return availability * minutes * rate


def _weighted_playing_stats(
    rows: Sequence[HistoricalFeatureRow],
    anchor: datetime,
    config: CohortConfig,
    scoring_policy: ScoringPolicy,
) -> tuple[float, float, float]:
    """Half-life-weighted (minutes, fantasy points per minute, sample), decayed from ``anchor``."""
    weighted_minutes: list[tuple[float, float]] = []
    weighted_rates: list[tuple[float, float]] = []
    for row in rows:
        if row.game_start >= anchor or not row.target_did_play or not row.target_minutes:
            continue
        weight = exp(
            -log(2)
            * max((anchor - row.game_start).total_seconds(), 0)
            / 86400
            / config.half_life_days
        )
        weighted_minutes.append((row.target_minutes, weight))
        weighted_rates.append(
            (
                calculate_fantasy_points(row.target_box_score, scoring_policy) / row.target_minutes,
                weight * row.target_minutes,
            )
        )
    if not weighted_minutes:
        return 0.0, 0.0, 0.0
    minutes = _weighted_mean(weighted_minutes)
    rate = _weighted_mean(weighted_rates)
    effective = sum(weight for _, weight in weighted_minutes)
    return minutes, rate, effective


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


def _season_start_year(value: datetime) -> int:
    return value.year if value.month >= 10 else value.year - 1


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
