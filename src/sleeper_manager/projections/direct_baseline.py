from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import exp, isfinite, log

from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


class ProjectionBaselineError(ValueError):
    pass


_DISABLED_ADJUSTMENTS = ("opponent", "pace", "rest", "travel", "injury")


@dataclass(frozen=True, slots=True)
class ProjectionBaselineConfig:
    recency_half_life_days: float = 14.0
    season_shrinkage_games: float = 5.0
    role_blend: float = 0.5
    percentiles: tuple[int, ...] = (10, 25, 50, 75, 90)
    disabled_adjustments: tuple[str, ...] = _DISABLED_ADJUSTMENTS

    def __post_init__(self) -> None:
        if not isfinite(self.recency_half_life_days) or self.recency_half_life_days <= 0:
            raise ProjectionBaselineError("Recency half-life must be finite and positive")
        if not isfinite(self.season_shrinkage_games) or self.season_shrinkage_games < 0:
            raise ProjectionBaselineError(
                "Season shrinkage strength must be finite and non-negative"
            )
        if not 0 <= self.role_blend <= 1:
            raise ProjectionBaselineError("Role blend must be between zero and one")
        if tuple(sorted(set(self.percentiles))) != self.percentiles:
            raise ProjectionBaselineError("Projection percentiles must be unique and ordered")
        if any(percentile < 0 or percentile > 100 for percentile in self.percentiles):
            raise ProjectionBaselineError("Projection percentiles must be between zero and 100")
        unsupported = set(self.disabled_adjustments) - set(_DISABLED_ADJUSTMENTS)
        if unsupported:
            raise ProjectionBaselineError(
                "Unknown deferred projection adjustments: " + ", ".join(sorted(unsupported))
            )

    @property
    def model_version(self) -> str:
        payload = {
            "recency_half_life_days": self.recency_half_life_days,
            "season_shrinkage_games": self.season_shrinkage_games,
            "role_blend": self.role_blend,
            "percentiles": self.percentiles,
            "disabled_adjustments": self.disabled_adjustments,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()[:12]
        return f"projection-baseline-v1-{fingerprint}"


class DirectFantasyPointBaseline:
    def __init__(self, config: ProjectionBaselineConfig | None = None) -> None:
        self.config = config or ProjectionBaselineConfig()
        self._indexes: dict[tuple[str, str], _HistoricalIndex] = {}

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = _find_target(dataset.rows, player_id=player_id, game_id=game_id)
        _validate_timestamp(target.available_as_of, "feature row available_as_of")
        index = self._index(dataset, scoring_policy, target)
        season = _season_key(target.game_start)
        prior_rows = index.player_rows_before(
            player_id,
            season,
            target.game_start,
        )
        season_mean = index.season_weighted_mean(
            season,
            target.game_start,
        )
        if season_mean is None:
            raise ProjectionBaselineError(
                f"No prior same-season observations for {player_id!r} before {game_id!r}"
            )

        player_observations = _production_observations(
            prior_rows, target, scoring_policy, self.config
        )
        if player_observations:
            player_mean = _weighted_mean(player_observations)
            effective_games = sum(weight for _, weight in player_observations)
            shrinkage = effective_games / (effective_games + self.config.season_shrinkage_games)
            expected = season_mean + shrinkage * (player_mean - season_mean)
            source_observations = player_observations
            source_mean = player_mean
            history_message = (
                f"Used {len(prior_rows)} prior same-season player games with "
                f"{effective_games:.2f} effective recency-weighted games."
            )
        else:
            season_rows = index.season_rows_before(season, target.game_start)
            season_observations = _direct_observations(
                season_rows,
                target,
                scoring_policy,
                self.config,
            )
            expected = season_mean
            shrinkage = 0.0
            source_observations = season_observations
            source_mean = expected
            history_message = (
                "No prior same-season player games were available; used the prior-only "
                "same-season league distribution."
            )
        shifted_observations = tuple(
            (value + expected - source_mean, weight) for value, weight in source_observations
        )
        distribution = ProjectionDistribution.from_weighted_observations(
            shifted_observations,
            percentiles=self.config.percentiles,
        )
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        reasons = _reasons(
            target=target,
            prior_rows=prior_rows,
            scoring_policy=scoring_policy,
            config=self.config,
            history_message=history_message,
            player_mean=player_mean if player_observations else None,
            season_mean=season_mean,
            shrinkage=shrinkage,
            distribution=distribution,
        )
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.config.model_version,
            input_version=_input_version(
                dataset,
                target,
                scoring_policy,
                history_fingerprint=index.fingerprint_before(target.game_start),
            ),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
        )

    def _index(
        self,
        dataset: HistoricalFeatureDataset,
        scoring_policy: ScoringPolicy,
        target: HistoricalFeatureRow,
    ) -> _HistoricalIndex:
        key = dataset.dataset_version, scoring_policy.version
        index = self._indexes.setdefault(
            key,
            _HistoricalIndex(
                scoring_policy=scoring_policy,
                half_life_days=self.config.recency_half_life_days,
            ),
        )
        point_in_time_count = getattr(dataset.rows, "prior_count", None)
        target_is_last = bool(dataset.rows) and dataset.rows[-1] == target
        prior_count = (
            point_in_time_count
            if isinstance(point_in_time_count, int)
            else len(dataset.rows) - int(target_is_last)
        )
        index.extend(dataset.rows, prior_count)
        return index


@dataclass(slots=True)
class _SeasonIndex:
    origin: datetime
    rows: list[HistoricalFeatureRow] = field(default_factory=list)
    starts: list[datetime] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    cumulative_weights: list[float] = field(default_factory=list)
    cumulative_weighted_scores: list[float] = field(default_factory=list)


@dataclass(slots=True)
class _HistoricalIndex:
    scoring_policy: ScoringPolicy
    half_life_days: float
    processed_count: int = 0
    rows: list[HistoricalFeatureRow] = field(default_factory=list)
    starts: list[datetime] = field(default_factory=list)
    prefix_fingerprints: list[str] = field(default_factory=list)
    seasons: dict[int, _SeasonIndex] = field(default_factory=dict)
    players: dict[tuple[str, int], list[HistoricalFeatureRow]] = field(default_factory=dict)

    def extend(self, rows: Sequence[HistoricalFeatureRow], prior_count: int) -> None:
        if prior_count < self.processed_count:
            return
        if prior_count == self.processed_count:
            return
        additions = rows[self.processed_count : prior_count]
        fingerprint = self.prefix_fingerprints[-1] if self.prefix_fingerprints else ""
        for row in additions:
            if self.starts and row.game_start < self.starts[-1]:
                raise ProjectionBaselineError("Historical rows must be evaluated chronologically")
            self.rows.append(row)
            self.starts.append(row.game_start)
            season = _season_key(row.game_start)
            season_index = self.seasons.setdefault(season, _SeasonIndex(row.game_start))
            season_index.rows.append(row)
            season_index.starts.append(row.game_start)
            score = calculate_fantasy_points(row.target_box_score, self.scoring_policy)
            season_index.scores.append(score)
            age_from_origin = (row.game_start - season_index.origin).total_seconds() / 86400
            transformed_weight = exp(log(2) * age_from_origin / self.half_life_days)
            season_index.cumulative_weights.append(
                transformed_weight
                + (season_index.cumulative_weights[-1] if season_index.cumulative_weights else 0)
            )
            season_index.cumulative_weighted_scores.append(
                score * transformed_weight
                + (
                    season_index.cumulative_weighted_scores[-1]
                    if season_index.cumulative_weighted_scores
                    else 0
                )
            )
            self.players.setdefault((row.player_id, season), []).append(row)
            fingerprint = hashlib.sha256(
                f"{fingerprint}:{_row_fingerprint(row)}".encode()
            ).hexdigest()
            self.prefix_fingerprints.append(fingerprint)
        self.processed_count = prior_count

    def player_rows_before(
        self,
        player_id: str,
        season: int,
        game_start: datetime,
    ) -> tuple[HistoricalFeatureRow, ...]:
        return tuple(
            row for row in self.players.get((player_id, season), ()) if row.game_start < game_start
        )

    def season_rows_before(
        self,
        season: int,
        game_start: datetime,
    ) -> tuple[HistoricalFeatureRow, ...]:
        index = self.seasons.get(season)
        if index is None:
            return ()
        return tuple(index.rows[: bisect_left(index.starts, game_start)])

    def season_weighted_mean(
        self,
        season: int,
        game_start: datetime,
    ) -> float | None:
        index = self.seasons.get(season)
        if index is None:
            return None
        count = bisect_left(index.starts, game_start)
        if not count:
            return None
        return index.cumulative_weighted_scores[count - 1] / index.cumulative_weights[count - 1]

    def fingerprint_before(self, game_start: datetime) -> str:
        count = bisect_left(self.starts, game_start)
        return self.prefix_fingerprints[count - 1] if count else "empty"


def _find_target(
    rows: Sequence[HistoricalFeatureRow], *, player_id: str, game_id: str
) -> HistoricalFeatureRow:
    if rows:
        latest = rows[-1]
        if latest.player_id == player_id and latest.game_id == game_id:
            return latest
    matches = tuple(row for row in rows if row.player_id == player_id and row.game_id == game_id)
    if len(matches) != 1:
        raise ProjectionBaselineError(
            f"Expected one feature row for player/game, found {len(matches)}"
        )
    return matches[0]


def _validate_timestamp(value: datetime, field: str) -> None:
    if value.tzinfo is None:
        raise ProjectionBaselineError(f"{field} must be timezone-aware")


def _season_key(value: datetime) -> int:
    return value.year if value.month >= 10 else value.year - 1


def _weight(target: HistoricalFeatureRow, row: HistoricalFeatureRow, half_life: float) -> float:
    age_days = max((target.game_start - row.game_start).total_seconds() / 86400, 0.0)
    return exp(-log(2) * age_days / half_life)


def _direct_observations(
    rows: Iterable[HistoricalFeatureRow],
    target: HistoricalFeatureRow,
    policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            calculate_fantasy_points(row.target_box_score, policy),
            _weight(target, row, config.recency_half_life_days),
        )
        for row in rows
    )


def _production_observations(
    rows: Iterable[HistoricalFeatureRow],
    target: HistoricalFeatureRow,
    policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
) -> tuple[tuple[float, float], ...]:
    records = tuple(rows)
    played = tuple(
        row
        for row in records
        if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
    )
    if not played:
        return ()
    minute_observations = tuple(
        (row.target_minutes or 0.0, _weight(target, row, config.recency_half_life_days))
        for row in played
    )
    expected_minutes = _weighted_mean(minute_observations)
    result: list[tuple[float, float]] = []
    for row in records:
        weight = _weight(target, row, config.recency_half_life_days)
        score = calculate_fantasy_points(row.target_box_score, policy)
        if row.target_did_play and row.target_minutes and row.target_minutes > 0:
            role_score = score / row.target_minutes * expected_minutes
            score = (1 - config.role_blend) * score + config.role_blend * role_score
        result.append((score, weight))
    return tuple(result)


def _weighted_mean(observations: Iterable[tuple[float, float]]) -> float:
    values = tuple(observations)
    total_weight = sum(weight for _, weight in values)
    if not values or total_weight <= 0:
        raise ProjectionBaselineError("Weighted projection observations are empty")
    return sum(value * weight for value, weight in values) / total_weight


def _reasons(
    *,
    target: HistoricalFeatureRow,
    prior_rows: tuple[HistoricalFeatureRow, ...],
    scoring_policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
    history_message: str,
    player_mean: float | None,
    season_mean: float,
    shrinkage: float,
    distribution: ProjectionDistribution,
) -> tuple[ProjectionReason, ...]:
    played = tuple(
        row
        for row in prior_rows
        if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
    )
    if played:
        weighted_minutes = _weighted_mean(
            (row.target_minutes or 0.0, _weight(target, row, config.recency_half_life_days))
            for row in played
        )
        starts = _weighted_mean(
            (float(row.target_started), _weight(target, row, config.recency_half_life_days))
            for row in played
        )
        role_message = (
            f"Blended direct production with points-per-minute at {weighted_minutes:.1f} "
            f"weighted minutes and a {starts:.0%} start rate."
        )
    else:
        role_message = "No played-minute history was available for a role adjustment."
    shrink_message = (
        f"Shrank player production {shrinkage:.0%} toward the prior-only same-season "
        f"league mean of {season_mean:.2f} fantasy points."
    )
    reasons = [
        ProjectionReason("recency", history_message),
        ProjectionReason("minutes_role", role_message),
        ProjectionReason(
            "season_shrinkage",
            shrink_message,
            adjustment=None if player_mean is None else distribution.expected_value - player_mean,
        ),
        ProjectionReason(
            "empirical_uncertainty",
            f"Used {len(distribution.weighted_observations)} weighted empirical outcomes "
            f"with variance {distribution.variance:.2f}.",
        ),
    ]
    for adjustment in config.disabled_adjustments:
        reasons.append(
            ProjectionReason(
                f"deferred_{adjustment}",
                f"{adjustment.title()} adjustment was not applied pending "
                "chronological backtesting.",
                applied=False,
            )
        )
    return tuple(reasons)


def _input_version(
    dataset: HistoricalFeatureDataset,
    target: HistoricalFeatureRow,
    policy: ScoringPolicy,
    *,
    history_fingerprint: str,
) -> str:
    payload = {
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "player_id": target.player_id,
        "game_id": target.game_id,
        "available_as_of": target.available_as_of.isoformat(),
        "scoring_policy_version": policy.version,
        "history_fingerprint": history_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"projection-input-v2-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _row_fingerprint(row: HistoricalFeatureRow) -> str:
    payload = {
        "game_id": row.game_id,
        "player_id": row.player_id,
        "game_start": row.game_start.isoformat(),
        "target_minutes": row.target_minutes,
        "target_started": row.target_started,
        "target_did_play": row.target_did_play,
        "target_box_score": (
            row.target_box_score.points,
            row.target_box_score.rebounds,
            row.target_box_score.assists,
            row.target_box_score.steals,
            row.target_box_score.blocks,
            row.target_box_score.turnovers,
            row.target_box_score.three_pointers_made,
            row.target_box_score.technical_fouls,
            row.target_box_score.flagrant_fouls,
        ),
        "source_lineage": [
            (source.provider, source.provider_id, source.content_hash)
            for source in row.source_lineage
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
