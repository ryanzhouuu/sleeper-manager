"""Interpretable direct fantasy-point projection math and public API compatibility."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
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
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline_history import (
    DirectBaselineObservation as DirectBaselineObservation,
)
from sleeper_manager.projections.direct_baseline_history import (
    PregameProjectionRequest as PregameProjectionRequest,
)
from sleeper_manager.projections.direct_baseline_history import (
    ProjectionBaselineError as ProjectionBaselineError,
)
from sleeper_manager.projections.direct_baseline_history import (
    _DirectBaselineHistoryIndex,
    _history_fingerprint,
    _pregame_input_version,
)
from sleeper_manager.projections.direct_baseline_history import (
    compact_direct_baseline_observations as compact_direct_baseline_observations,
)

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
        return _cached_baseline_version(self)


@lru_cache(maxsize=128)
def _cached_baseline_version(config: ProjectionBaselineConfig) -> str:
    """Memoize the pure config digest; eviction only recomputes identical values."""
    payload = {
        "recency_half_life_days": config.recency_half_life_days,
        "season_shrinkage_games": config.season_shrinkage_games,
        "role_blend": config.role_blend,
        "percentiles": config.percentiles,
        "disabled_adjustments": config.disabled_adjustments,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()[:12]
    return f"projection-baseline-v1-{fingerprint}"


class DirectFantasyPointBaseline:
    """Project empirical fantasy-point distributions from finalized prior outcomes."""

    def __init__(self, config: ProjectionBaselineConfig | None = None) -> None:
        """Create independent explicit-history and incremental backtest indexes."""
        self.config = config or ProjectionBaselineConfig()
        self._pregame_indexes: dict[tuple[str, str, str], _DirectBaselineHistoryIndex] = {}
        self._backtest_indexes: dict[tuple[str, str, str], _DirectBaselineHistoryIndex] = {}

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        """Project one historical target without admitting its realized outcome."""
        prior_count: object = getattr(dataset.rows, "prior_count", None)
        if isinstance(prior_count, int) and not isinstance(prior_count, bool):
            index = self._backtest_index(dataset, scoring_policy)
            target = index.resolve_historical_target(
                dataset.rows,
                player_id=player_id,
                game_id=game_id,
            )
            request = self._historical_request(dataset, target)
            return self._project_prepared(
                request,
                index,
                scoring_policy=scoring_policy,
                exceed_score=exceed_score,
            )
        target = _find_target(dataset.rows, player_id=player_id, game_id=game_id)
        request = self._historical_request(
            dataset,
            target,
            history=tuple(
                DirectBaselineObservation.from_historical_row(row)
                for row in dataset.rows
                if row.game_start < target.game_start
            ),
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
        """Project from an explicit compact history validated at a pregame cutoff."""
        index = self._pregame_index(request, scoring_policy)
        return self._project_prepared(
            request,
            index,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )

    def _project_prepared(
        self,
        request: PregameProjectionRequest,
        index: _DirectBaselineHistoryIndex,
        *,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None,
    ) -> ProjectionSnapshot:
        """Apply shared direct-projection math to one validated request and history index."""
        season = nba_season_start_year(request.game_start)
        prior_rows = index.player_rows_before(
            request.history_player_id or request.player_id,
            season,
            request.game_start,
            request.available_as_of,
        )
        season_mean = index.season_weighted_mean(
            season,
            request.game_start,
            request.available_as_of,
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
            season_rows = index.season_rows_before(
                season,
                request.game_start,
                request.available_as_of,
            )
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
                history_fingerprint=index.fingerprint_before(
                    request.game_start,
                    request.available_as_of,
                ),
            ),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
        )

    def _pregame_index(
        self,
        request: PregameProjectionRequest,
        scoring_policy: ScoringPolicy,
    ) -> _DirectBaselineHistoryIndex:
        history_fingerprint = _history_fingerprint(request.history)
        key = request.dataset_version, scoring_policy.version, history_fingerprint
        index = self._pregame_indexes.setdefault(
            key,
            _DirectBaselineHistoryIndex(
                scoring_policy=scoring_policy,
                half_life_days=self.config.recency_half_life_days,
            ),
        )
        index.extend(request.history, len(request.history))
        return index

    def _backtest_index(
        self,
        dataset: HistoricalFeatureDataset,
        scoring_policy: ScoringPolicy,
    ) -> _DirectBaselineHistoryIndex:
        """Return the isolated incremental index for one modeled input identity."""
        key = dataset.dataset_version, scoring_policy.version, self.config.model_version
        return self._backtest_indexes.setdefault(
            key,
            _DirectBaselineHistoryIndex(
                scoring_policy=scoring_policy,
                half_life_days=self.config.recency_half_life_days,
            ),
        )

    @staticmethod
    def _historical_request(
        dataset: HistoricalFeatureDataset,
        target: HistoricalFeatureRow,
        *,
        history: tuple[DirectBaselineObservation, ...] = (),
    ) -> PregameProjectionRequest:
        """Translate a sanitized historical target to production-neutral request metadata."""
        return PregameProjectionRequest(
            dataset_version=dataset.dataset_version,
            feature_schema_version=dataset.feature_schema_version,
            player_id=target.sleeper_id or target.player_id,
            game_id=target.game_id,
            game_start=target.game_start,
            available_as_of=target.available_as_of,
            history=history,
            history_player_id=target.player_id,
            source_versions=dataset.source_versions,
        )


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


def _weight(target_start: datetime, row: DirectBaselineObservation, half_life: float) -> float:
    age_days = max((target_start - row.game_start).total_seconds() / 86400, 0.0)
    return exp(-log(2) * age_days / half_life)


def _direct_observations(
    rows: Iterable[DirectBaselineObservation],
    target_start: datetime,
    policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            calculate_fantasy_points(row.box_score, policy),
            _weight(target_start, row, config.recency_half_life_days),
        )
        for row in rows
    )


def _production_observations(
    rows: Iterable[DirectBaselineObservation],
    target_start: datetime,
    policy: ScoringPolicy,
    config: ProjectionBaselineConfig,
) -> tuple[tuple[float, float], ...]:
    records = tuple(rows)
    played = tuple(
        row
        for row in records
        if not row.did_not_play and row.minutes is not None and row.minutes > 0
    )
    if not played:
        return ()
    minute_observations = tuple(
        (
            row.minutes or 0.0,
            _weight(target_start, row, config.recency_half_life_days),
        )
        for row in played
    )
    expected_minutes = _weighted_mean(minute_observations)
    result: list[tuple[float, float]] = []
    for row in records:
        weight = _weight(target_start, row, config.recency_half_life_days)
        score = calculate_fantasy_points(row.box_score, policy)
        if not row.did_not_play and row.minutes and row.minutes > 0:
            role_score = score / row.minutes * expected_minutes
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
    prior_rows: tuple[DirectBaselineObservation, ...],
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
        if not row.did_not_play and row.minutes is not None and row.minutes > 0
    )
    if played:
        weighted_minutes = _weighted_mean(
            (
                row.minutes or 0.0,
                _weight(target_start, row, config.recency_half_life_days),
            )
            for row in played
        )
        starts = _weighted_mean(
            (
                float(row.started),
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
