"""Cutoff-safe seasonal evidence and statistical-profile donor selection.

Pools retain player-game identities even when stat lines coincide. Preparation
is shared across player targets at one cutoff and never reads future outcomes.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import exp, fsum, log

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import (
    STAT_FIELDS,
    HybridProjectionConfig,
    HybridProjectionError,
)
from sleeper_manager.projections.hybrid_profiles import PlayerStatProfile, StatProfilePopulation
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    require_aware,
)

WeightedGame = tuple[JointGameObservation, float]


@dataclass(frozen=True, slots=True)
class JointStatPool:
    """Normalized probabilities over distinct complete played-game outcomes."""

    samples: tuple[JointGameObservation, ...]
    weights: tuple[float, ...]
    donor_ids: tuple[str, ...]
    history_kind: str

    @property
    def means(self) -> tuple[float, ...]:
        return stat_means(self.samples, self.weights)


def stat_means(
    samples: tuple[JointGameObservation, ...], weights: tuple[float, ...]
) -> tuple[float, ...]:
    """Return weighted core-stat means; weights must be normalized by the caller."""
    return tuple(
        fsum(row.core_stats[k] * weight for row, weight in zip(samples, weights, strict=True))
        for k in range(7)
    )


def seasonal_games(
    rows: tuple[JointGameObservation, ...],
    cutoff: datetime,
    season: int,
    config: HybridProjectionConfig,
) -> tuple[WeightedGame, ...]:
    """Preserve opening-night priors, then fade their mass as verified current games accrue."""
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        counts[nba_season_start_year(row.game_start)] += 1
    result: list[WeightedGame] = []
    for row in rows:
        age = season - nba_season_start_year(row.game_start)
        if age == 0:
            days = (cutoff - row.game_start).total_seconds() / 86400
            weight = exp(-log(2) * days / config.recency_half_life_days)
        elif age in (1, 2):
            strength = config.previous_season_strength if age == 1 else config.older_season_strength
            count = counts[season - age]
            weight = min(strength, count) / count * strength / (strength + counts[season])
        else:
            continue
        if weight > 0:
            result.append((row, weight))
    return tuple(result)


class HybridHistoryPools:
    """Prepare source-covered profiles once for one cutoff, season, and scoring policy."""

    def __init__(
        self,
        history: HybridHistory,
        *,
        cutoff: datetime,
        game_start: datetime,
        scoring_policy: ScoringPolicy,
        config: HybridProjectionConfig,
    ) -> None:
        require_aware(cutoff)
        require_aware(game_start)
        self.config = config
        self.season = nba_season_start_year(game_start)
        required = set(STAT_FIELDS)
        if scoring_policy.technical_foul:
            required.add("technical_fouls")
        if scoring_policy.flagrant_foul:
            required.add("flagrant_fouls")
        by_player: dict[str, list[JointGameObservation]] = defaultdict(list)
        self.incomplete_players: set[str] = set()
        self.excluded_games: list[tuple[str, str]] = []
        for row in sorted(
            history.observations, key=lambda row: (row.game_start, row.player_id, row.game_id)
        ):
            age = self.season - nba_season_start_year(row.game_start)
            if row.finalized_at > cutoff or row.game_start >= cutoff or age not in (0, 1, 2):
                continue
            if not row.did_play:
                continue
            if (
                row.minutes is None
                or row.minutes <= 0
                or not required.issubset(row.verified_fields)
            ):
                self.incomplete_players.add(row.player_id)
                self.excluded_games.append((row.player_id, row.game_id))
                continue
            by_player[row.player_id].append(row)
        self.games = {
            player: seasonal_games(tuple(rows), cutoff, self.season, config)
            for player, rows in sorted(by_player.items())
        }
        self.profiles = {player: self._profile(rows) for player, rows in self.games.items()}
        self.population = StatProfilePopulation(
            tuple(
                PlayerStatProfile(
                    player, len(rows), fsum(weight for _, weight in rows), self.profiles[player]
                )
                for player, rows in self.games.items()
            ),
            config,
        )
        self.scales = self.population.scales

    def pool(self, player_id: str, center: tuple[float, ...] | None = None) -> JointStatPool:
        """External lookup uses its center; internal lookup independently uses historical priors."""
        own = self.games.get(player_id, ())
        if not own and player_id in self.incomplete_players:
            raise HybridProjectionError("unverified_player_stats")
        own_strength = fsum(weight for _, weight in own)
        donor_ids = self.population.donors(player_id, center)
        own_scale = 1.0
        if own and donor_ids:
            cap = self.config.maximum_game_weight
            largest = max(weight for _, weight in own)
            if largest > cap * (own_strength + self.config.donor_strength):
                # Solve largest*a / (own_strength*a + donor_strength) <= cap.
                own_scale = cap * self.config.donor_strength / (largest - cap * own_strength)
        combined = [(row, weight * own_scale) for row, weight in own]
        for donor in donor_ids:
            rows = self.games[donor]
            mass = self.config.donor_strength / len(donor_ids)
            total = fsum(weight for _, weight in rows)
            combined.extend((row, weight * mass / total) for row, weight in rows)
        if not combined:
            raise HybridProjectionError("missing_donor_support")
        total = fsum(weight for _, weight in combined)
        ages = {self.season - nba_season_start_year(row.game_start) for row, _ in own}
        kind = (
            "current_season"
            if 0 in ages
            else "prior_season"
            if 1 in ages
            else "older_history"
            if own
            else "no_history"
        )
        return JointStatPool(
            tuple(row for row, _ in combined),
            tuple(weight / total for _, weight in combined),
            donor_ids,
            kind,
        )

    @staticmethod
    def _profile(rows: tuple[WeightedGame, ...]) -> tuple[float, ...]:
        total = fsum(weight for _, weight in rows)
        return stat_means(
            tuple(row for row, _ in rows), tuple(weight / total for _, weight in rows)
        )
