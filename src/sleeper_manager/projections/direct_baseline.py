from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import exp, isfinite, log

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.domain.statistics import weighted_mean
from sleeper_manager.integrations.nba.historical_feature_models import (
    DatasetSourceVersion,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


class ProjectionBaselineError(ValueError):
    def __init__(self, message: str, *, reason_code: str = "projection_baseline_error") -> None:
        super().__init__(message)
        self.reason_code = reason_code


MISSING_WARMUP_REASON = "missing_warmup_history"


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


@dataclass(frozen=True, slots=True)
class PregameProjectionRequest:
    """Outcome-free projection context available before one game's tipoff."""

    dataset_version: str
    feature_schema_version: str
    player_id: str
    game_id: str
    game_start: datetime
    available_as_of: datetime
    history: tuple[HistoricalFeatureRow, ...]
    history_player_id: str | None = None
    source_versions: tuple[DatasetSourceVersion, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("dataset_version", self.dataset_version),
            ("feature_schema_version", self.feature_schema_version),
            ("player_id", self.player_id),
            ("game_id", self.game_id),
        ):
            if not value.strip():
                raise ProjectionBaselineError(f"Pregame {label} must be non-empty")
        _validate_timestamp(self.game_start, "pregame game_start")
        _validate_timestamp(self.available_as_of, "pregame available_as_of")
        history_player_id = self.history_player_id or self.player_id
        if not history_player_id.strip():
            raise ProjectionBaselineError("Pregame history_player_id must be non-empty")
        object.__setattr__(self, "history_player_id", history_player_id)
        if self.available_as_of > self.game_start:
            raise ProjectionBaselineError(
                "Pregame projection availability cannot follow game start"
            )
        for row in self.history:
            if row.game_start.tzinfo is None:
                raise ProjectionBaselineError("Pregame history game starts must be timezone-aware")
            if row.outcome_finalized_at is not None and row.outcome_finalized_at.tzinfo is None:
                raise ProjectionBaselineError("Pregame outcome finalization must be timezone-aware")
        candidate_history = tuple(
            row
            for row in self.history
            if row.game_start < self.game_start
            and row.outcome_finalized_at is not None
            and row.outcome_finalized_at <= self.available_as_of
        )
        history = tuple(sorted(candidate_history, key=_history_sort_key))
        object.__setattr__(self, "history", history)


class DirectFantasyPointBaseline:
    def __init__(self, config: ProjectionBaselineConfig | None = None) -> None:
        self.config = config or ProjectionBaselineConfig()
        self._pregame_indexes: dict[tuple[str, str, str], _HistoricalIndex] = {}

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
        request = PregameProjectionRequest(
            dataset_version=dataset.dataset_version,
            feature_schema_version=dataset.feature_schema_version,
            player_id=target.sleeper_id or target.player_id,
            game_id=target.game_id,
            game_start=target.game_start,
            available_as_of=target.available_as_of,
            history=tuple(row for row in dataset.rows if row.game_start < target.game_start),
            history_player_id=target.player_id,
            source_versions=dataset.source_versions,
        )
        return self.project_pregame(
            request,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )

    def project_pregame(
        self,
        request: PregameProjectionRequest,
        *,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        index = self._pregame_index(request, scoring_policy)
        season = nba_season_start_year(request.game_start)
        prior_rows = index.player_rows_before(
            request.history_player_id or request.player_id,
            season,
            request.game_start,
        )
        season_mean = index.season_weighted_mean(
            season,
            request.game_start,
        )
        if season_mean is None:
            raise ProjectionBaselineError(
                f"No prior same-season observations for {request.player_id!r} "
                f"before {request.game_id!r}",
                reason_code=MISSING_WARMUP_REASON,
            )

        player_observations = _production_observations(
            prior_rows, request.game_start, scoring_policy, self.config
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
            season_rows = index.season_rows_before(season, request.game_start)
            season_observations = _direct_observations(
                season_rows,
                request.game_start,
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
            target_start=request.game_start,
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
            player_id=request.player_id,
            game_id=request.game_id,
            available_as_of=request.available_as_of,
            model_version=self.config.model_version,
            input_version=_pregame_input_version(
                request,
                scoring_policy,
                history_fingerprint=index.fingerprint_before(request.game_start),
            ),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
        )

    def _pregame_index(
        self,
        request: PregameProjectionRequest,
        scoring_policy: ScoringPolicy,
    ) -> _HistoricalIndex:
        history_fingerprint = _history_fingerprint(request.history)
        key = request.dataset_version, scoring_policy.version, history_fingerprint
        index = self._pregame_indexes.setdefault(
            key,
            _HistoricalIndex(
                scoring_policy=scoring_policy,
                half_life_days=self.config.recency_half_life_days,
            ),
        )
        index.extend(request.history, len(request.history))
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
            season = nba_season_start_year(row.game_start)
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


def _weight(target_start: datetime, row: HistoricalFeatureRow, half_life: float) -> float:
    age_days = max((target_start - row.game_start).total_seconds() / 86400, 0.0)
    return exp(-log(2) * age_days / half_life)


def _direct_observations(
    rows: Iterable[HistoricalFeatureRow],
    target_start: datetime,
    policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            calculate_fantasy_points(row.target_box_score, policy),
            _weight(target_start, row, config.recency_half_life_days),
        )
        for row in rows
    )


def _production_observations(
    rows: Iterable[HistoricalFeatureRow],
    target_start: datetime,
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
        (
            row.target_minutes or 0.0,
            _weight(target_start, row, config.recency_half_life_days),
        )
        for row in played
    )
    expected_minutes = _weighted_mean(minute_observations)
    result: list[tuple[float, float]] = []
    for row in records:
        weight = _weight(target_start, row, config.recency_half_life_days)
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
    return weighted_mean(values)


def _reasons(
    *,
    target_start: datetime,
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
            (
                row.target_minutes or 0.0,
                _weight(target_start, row, config.recency_half_life_days),
            )
            for row in played
        )
        starts = _weighted_mean(
            (
                float(row.target_started),
                _weight(target_start, row, config.recency_half_life_days),
            )
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


def _pregame_input_version(
    request: PregameProjectionRequest,
    policy: ScoringPolicy,
    *,
    history_fingerprint: str,
) -> str:
    payload = {
        "dataset_version": request.dataset_version,
        "feature_schema_version": request.feature_schema_version,
        "player_id": request.player_id,
        "history_player_id": request.history_player_id,
        "game_id": request.game_id,
        "game_start": request.game_start.isoformat(),
        "available_as_of": request.available_as_of.isoformat(),
        "outcome_finalized_at": [
            (row.game_id, row.outcome_finalized_at.isoformat())
            for row in request.history
            if row.outcome_finalized_at is not None
        ],
        "source_versions": [
            (source.provider, source.schema_version, source.source_ids)
            for source in request.source_versions
        ],
        "scoring_policy_version": policy.version,
        "history_fingerprint": history_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"projection-input-v3-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _history_sort_key(row: HistoricalFeatureRow) -> tuple[datetime, str, str]:
    return row.game_start, row.game_id, row.player_id


def _history_fingerprint(rows: Sequence[HistoricalFeatureRow]) -> str:
    fingerprint = "empty"
    for row in rows:
        fingerprint = hashlib.sha256(f"{fingerprint}:{_row_fingerprint(row)}".encode()).hexdigest()
    return fingerprint


def _row_fingerprint(row: HistoricalFeatureRow) -> str:
    payload = {
        "game_id": row.game_id,
        "player_id": row.player_id,
        "game_start": row.game_start.isoformat(),
        "target_minutes": row.target_minutes,
        "target_started": row.target_started,
        "target_did_play": row.target_did_play,
        "outcome_finalized_at": (
            row.outcome_finalized_at.isoformat() if row.outcome_finalized_at is not None else None
        ),
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
