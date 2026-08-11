from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import exp, isfinite, log

from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_features import (
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
        prior_rows = tuple(
            row
            for row in dataset.rows
            if row.player_id == player_id
            and row.game_start < target.game_start
            and _season_key(row.game_start) == _season_key(target.game_start)
        )
        season_rows = tuple(
            row
            for row in dataset.rows
            if row.game_start < target.game_start
            and _season_key(row.game_start) == _season_key(target.game_start)
        )
        if not season_rows:
            raise ProjectionBaselineError(
                f"No prior same-season observations for {player_id!r} before {game_id!r}"
            )

        player_observations = _production_observations(
            prior_rows, target, scoring_policy, self.config
        )
        season_observations = _direct_observations(season_rows, target, scoring_policy, self.config)
        if player_observations:
            player_mean = _weighted_mean(player_observations)
            season_mean = _weighted_mean(season_observations)
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
            expected = _weighted_mean(season_observations)
            season_mean = expected
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
            input_version=_input_version(dataset, target, prior_rows, season_rows, scoring_policy),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
        )


def _find_target(
    rows: Iterable[HistoricalFeatureRow], *, player_id: str, game_id: str
) -> HistoricalFeatureRow:
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
    prior_rows: Iterable[HistoricalFeatureRow],
    season_rows: Iterable[HistoricalFeatureRow],
    policy: ScoringPolicy,
) -> str:
    rows = tuple(
        sorted(
            set(prior_rows) | set(season_rows),
            key=lambda row: (row.game_start, row.player_id, row.game_id),
        )
    )
    payload = {
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "player_id": target.player_id,
        "game_id": target.game_id,
        "available_as_of": target.available_as_of.isoformat(),
        "scoring_policy_version": policy.version,
        "historical_inputs": [
            {
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
            for row in rows
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"projection-input-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"
