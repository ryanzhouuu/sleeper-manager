"""Compact immutable preparation for a season; exact joint outcomes remain in the history store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, fsum, isfinite, log

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import (
    STAT_FIELDS,
    HybridProjectionConfig,
    HybridProjectionError,
)
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch
from sleeper_manager.projections.hybrid_participation_counts import ParticipationCounts
from sleeper_manager.projections.hybrid_pools import HybridHistoryPools, seasonal_games
from sleeper_manager.projections.hybrid_profiles import PlayerStatProfile, StatProfilePopulation
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    require_aware,
)


@dataclass(frozen=True, slots=True)
class IndexedPlayerProfile:
    """Current-season mass decays uniformly after the preparation cutoff; prior mass is fixed."""

    player_id: str
    count: int
    current_mass: float
    old_mass: float
    current_totals: tuple[float, ...]
    old_totals: tuple[float, ...]
    reference_means: tuple[float, ...]

    def __post_init__(self) -> None:
        """Require a complete finite profile before it can influence donor selection."""
        values = (
            self.current_mass,
            self.old_mass,
            *self.current_totals,
            *self.old_totals,
            *self.reference_means,
        )
        if (
            not self.player_id
            or self.count <= 0
            or any(
                len(row) != 7
                for row in (self.current_totals, self.old_totals, self.reference_means)
            )
            or any(not isfinite(value) or value < 0 for value in values)
            or self.current_mass + self.old_mass <= 0
        ):
            raise HybridProjectionError("invalid_indexed_profile")

    def at_decay(self, decay: float) -> PlayerStatProfile:
        """Preserve the reference profile exactly when recency has no effect."""
        mass = self.current_mass * decay + self.old_mass
        means = (
            self.reference_means
            if decay == 1 or self.current_mass == 0
            else tuple(
                (self.current_totals[k] * decay + self.old_totals[k]) / mass for k in range(7)
            )
        )
        return PlayerStatProfile(self.player_id, self.count, mass, means)


@dataclass(frozen=True, slots=True)
class HybridRuntimeIndex:
    """Bind a frozen evidence cutoff to its complete population, census counts, and exclusions."""

    dataset_version: str
    season: int
    reference_time: datetime
    scoring_policy_version: str
    model_version: str
    history_fingerprint: str
    profiles: tuple[IndexedPlayerProfile, ...]
    participation: tuple[tuple[str, ParticipationCounts], ...]
    incomplete_players: tuple[str, ...]
    excluded_games: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require_aware(self.reference_time)
        if not all(
            (
                self.dataset_version,
                self.scoring_policy_version,
                self.model_version,
                self.history_fingerprint,
            )
        ):
            raise HybridProjectionError("missing_runtime_index_identity")
        if len({row.player_id for row in self.profiles}) != len(self.profiles):
            raise HybridProjectionError("duplicate_index_profile")
        if len({player for player, _ in self.participation}) != len(self.participation):
            raise HybridProjectionError("duplicate_index_participation")

    def population(self, cutoff: datetime, config: HybridProjectionConfig) -> StatProfilePopulation:
        """Reject backdated reads and model mismatches instead of applying future preparation."""
        require_aware(cutoff)
        if cutoff < self.reference_time:
            raise HybridProjectionError("runtime_index_after_cutoff")
        if config.model_version != self.model_version:
            raise HybridProjectionError("runtime_index_model_mismatch")
        days = (cutoff - self.reference_time).total_seconds() / 86400
        decay = exp(-log(2) * days / config.recency_half_life_days)
        return StatProfilePopulation(tuple(row.at_decay(decay) for row in self.profiles), config)


def build_hybrid_runtime_index(
    history: HybridHistory,
    *,
    reference_time: datetime,
    game_start: datetime,
    scoring_policy: ScoringPolicy,
    config: HybridProjectionConfig | None = None,
) -> HybridRuntimeIndex:
    """Prepare offline once, preserving all eligible donor profiles and explicit census outcomes."""
    prepared = HybridProjectionBatch(
        history,
        cutoff=reference_time,
        game_start=game_start,
        scoring_policy=scoring_policy,
        config=config,
    )
    summaries = []
    for player, rows in prepared.pools.games.items():
        current = tuple(
            (row, weight)
            for row, weight in rows
            if nba_season_start_year(row.game_start) == prepared.season
        )
        old = tuple(
            (row, weight)
            for row, weight in rows
            if nba_season_start_year(row.game_start) != prepared.season
        )
        summaries.append(
            IndexedPlayerProfile(
                player,
                len(rows),
                fsum(weight for _, weight in current),
                fsum(weight for _, weight in old),
                tuple(
                    fsum(row.core_stats[k] * weight for row, weight in current) for k in range(7)
                ),
                tuple(fsum(row.core_stats[k] * weight for row, weight in old) for k in range(7)),
                prepared.pools.profiles[player],
            )
        )
    return HybridRuntimeIndex(
        history.dataset_version,
        prepared.season,
        reference_time,
        scoring_policy.version,
        prepared.config.model_version,
        prepared.history_fingerprint,
        tuple(summaries),
        tuple(sorted(prepared.participation.counts.items())),
        tuple(sorted(prepared.pools.incomplete_players)),
        tuple(prepared.pools.excluded_games),
    )


class IndexedHistoryPools(HybridHistoryPools):
    """Use the complete compact population with only this target's selected outcome partitions."""

    def __init__(
        self,
        index: HybridRuntimeIndex,
        population: StatProfilePopulation,
        observations: dict[str, tuple[JointGameObservation, ...]],
        *,
        cutoff: datetime,
        scoring_policy: ScoringPolicy,
        config: HybridProjectionConfig,
    ) -> None:
        if scoring_policy.version != index.scoring_policy_version:
            raise HybridProjectionError("runtime_index_scoring_mismatch")
        self.config, self.season, self.population = config, index.season, population
        self.incomplete_players = set(index.incomplete_players)
        self.excluded_games = list(index.excluded_games)
        self.scales = population.scales
        self.profiles = {player: row.means for player, row in population.profiles.items()}
        required = set(STAT_FIELDS)
        if scoring_policy.technical_foul:
            required.add("technical_fouls")
        if scoring_policy.flagrant_foul:
            required.add("flagrant_fouls")
        self.games = {
            player: seasonal_games(
                tuple(
                    sorted(
                        (
                            row
                            for row in rows
                            if row.game_start < index.reference_time
                            and row.finalized_at <= index.reference_time
                            and self.season - nba_season_start_year(row.game_start) in (0, 1, 2)
                            and row.did_play
                            and row.minutes is not None
                            and row.minutes > 0
                            and required.issubset(row.verified_fields)
                        ),
                        key=lambda row: (row.game_start, row.player_id, row.game_id),
                    )
                ),
                cutoff,
                self.season,
                config,
            )
            for player, rows in observations.items()
        }
