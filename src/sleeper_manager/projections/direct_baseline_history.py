"""History contracts, indexing, and provenance for direct fantasy-point projections.

The direct projection model owns distribution math in ``direct_baseline``. This module
owns compact historical observations, point-in-time request validation, and prepared
history indexes shared by explicit-history and backtest callers.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from math import exp, isfinite, log

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
    DatasetSourceVersion,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.incremental_history import (
    IncrementalHistory,
    IncrementalHistoryError,
)


class ProjectionBaselineError(ValueError):
    """Expose a stable reason code for unavailable or incompatible baseline inputs."""

    def __init__(self, message: str, *, reason_code: str = "projection_baseline_error") -> None:
        """Attach a machine-readable reason without changing normal ValueError behavior."""
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class DirectBaselineObservation:
    """Outcome data retained by the production direct-baseline history."""

    player_id: str
    game_id: str
    game_start: datetime
    outcome_finalized_at: datetime | None
    minutes: float | None
    started: bool
    did_not_play: bool
    box_score: BoxScoreLine
    source_version: str

    def __post_init__(self) -> None:
        """Reject observations that cannot participate in chronological provenance."""
        if not self.player_id.strip() or not self.game_id.strip():
            raise ProjectionBaselineError("Projection observations require player and game IDs")
        _validate_timestamp(self.game_start, "observation game_start")
        if self.outcome_finalized_at is not None:
            _validate_timestamp(self.outcome_finalized_at, "observation outcome_finalized_at")
        if self.minutes is not None and (not isfinite(self.minutes) or self.minutes < 0):
            raise ProjectionBaselineError("Projection observation minutes must be non-negative")
        if not self.source_version.strip():
            raise ProjectionBaselineError("Projection observations require a source version")

    @classmethod
    @lru_cache(maxsize=131072)
    def from_historical_row(cls, row: HistoricalFeatureRow) -> DirectBaselineObservation:
        """Compact a feature row to the finalized outcome fields used by the baseline."""
        source_version = hashlib.sha256(
            repr(
                tuple(
                    (source.provider, source.provider_id, source.content_hash)
                    for source in row.source_lineage
                )
            ).encode()
        ).hexdigest()
        return cls(
            player_id=row.player_id,
            game_id=row.game_id,
            game_start=row.game_start,
            outcome_finalized_at=row.outcome_finalized_at,
            minutes=row.target_minutes,
            started=row.target_started,
            did_not_play=not row.target_did_play,
            box_score=row.target_box_score,
            source_version=source_version,
        )


def compact_direct_baseline_observations(
    rows: Iterable[DirectBaselineObservation | HistoricalFeatureRow],
) -> tuple[DirectBaselineObservation, ...]:
    """Normalize production and historical rows to compact direct observations."""
    return tuple(
        row
        if isinstance(row, DirectBaselineObservation)
        else DirectBaselineObservation.from_historical_row(row)
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class PregameProjectionRequest:
    """Outcome-free projection context available before one game's tipoff."""

    dataset_version: str
    feature_schema_version: str
    player_id: str
    game_id: str
    game_start: datetime
    available_as_of: datetime
    history: tuple[DirectBaselineObservation, ...]
    history_player_id: str | None = None
    source_versions: tuple[DatasetSourceVersion, ...] = ()

    def __post_init__(self) -> None:
        """Normalize history and exclude evidence unavailable by the request cutoff."""
        object.__setattr__(
            self,
            "history",
            compact_direct_baseline_observations(self.history),
        )
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


@dataclass(slots=True)
class _SeasonIndex:
    """Hold one season's chronological rows and recency-weight prefix aggregates."""

    origin: datetime
    rows: list[DirectBaselineObservation] = field(default_factory=list)
    starts: list[datetime] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    cumulative_weights: list[float] = field(default_factory=list)
    cumulative_weighted_scores: list[float] = field(default_factory=list)


@dataclass(slots=True)
class _DirectBaselineHistoryIndex:
    """Prepare compact direct history for repeated point-in-time projection queries."""

    scoring_policy: ScoringPolicy
    half_life_days: float
    processed_count: int = 0
    rows: list[DirectBaselineObservation] = field(default_factory=list)
    starts: list[datetime] = field(default_factory=list)
    prefix_fingerprints: list[str] = field(default_factory=list)
    prefix_latest_finalization: list[datetime | None] = field(default_factory=list)
    seasons: dict[int, _SeasonIndex] = field(default_factory=dict)
    players: dict[tuple[str, int], list[DirectBaselineObservation]] = field(default_factory=dict)
    rows_by_key: dict[tuple[str, str], DirectBaselineObservation] = field(default_factory=dict)
    duplicate_keys: set[tuple[str, str]] = field(default_factory=set)
    historical_rows: IncrementalHistory[HistoricalFeatureRow] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Configure shared prefix synchronization with a content boundary fallback."""
        self.historical_rows = IncrementalHistory(
            timestamp=lambda row: row.game_start,
            same_boundary=_same_historical_boundary,
        )

    def extend(self, rows: Sequence[DirectBaselineObservation], prior_count: int) -> None:
        """Commit an explicit chronological observation prefix once per index."""
        if prior_count < self.processed_count:
            return
        if prior_count == self.processed_count:
            return
        for row in rows[self.processed_count : prior_count]:
            if self.starts and row.game_start < self.starts[-1]:
                raise ProjectionBaselineError("Historical rows must be evaluated chronologically")
            self._commit(row)
        self.processed_count = prior_count

    def resolve_historical_target(
        self,
        rows: Sequence[HistoricalFeatureRow],
        *,
        player_id: str,
        game_id: str,
    ) -> HistoricalFeatureRow:
        """Synchronize a growing prefix and resolve its uncommitted sanitized target."""
        try:
            limit = self.historical_rows.synchronize(
                rows,
                commit=self._commit_historical_row,
                reset=self._reset,
            )
        except IncrementalHistoryError as error:
            raise ProjectionBaselineError(str(error)) from error
        self.processed_count = limit
        if limit != len(rows) - 1:
            raise ProjectionBaselineError(
                "Point-in-time direct history must expose exactly one uncommitted target"
            )
        target = rows[limit]
        if target.player_id != player_id or target.game_id != game_id:
            raise ProjectionBaselineError("Point-in-time direct target identity does not match")
        return target

    def _commit_historical_row(self, row: HistoricalFeatureRow) -> None:
        """Compact one newly visible feature row before updating direct indexes."""
        self._commit(DirectBaselineObservation.from_historical_row(row))

    def _commit(self, row: DirectBaselineObservation) -> None:
        """Update every direct-history lookup and rolling aggregate for one observation."""
        key = row.player_id, row.game_id
        if key in self.rows_by_key:
            self.duplicate_keys.add(key)
            raise ProjectionBaselineError(
                f"Duplicate direct history observation for player/game {key!r}"
            )
        self.rows_by_key[key] = row
        self.rows.append(row)
        self.starts.append(row.game_start)
        season = nba_season_start_year(row.game_start)
        season_index = self.seasons.setdefault(season, _SeasonIndex(row.game_start))
        season_index.rows.append(row)
        season_index.starts.append(row.game_start)
        score = calculate_fantasy_points(row.box_score, self.scoring_policy)
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
        fingerprint = self.prefix_fingerprints[-1] if self.prefix_fingerprints else ""
        self.prefix_fingerprints.append(
            hashlib.sha256(f"{fingerprint}:{_row_fingerprint(row)}".encode()).hexdigest()
        )
        prior_latest = self.prefix_latest_finalization[-1] if len(self.rows) > 1 else None
        latest = row.outcome_finalized_at
        if prior_latest is not None and (latest is None or prior_latest > latest):
            latest = prior_latest
        self.prefix_latest_finalization.append(latest)

    def player_rows_before(
        self,
        player_id: str,
        season: int,
        game_start: datetime,
        available_as_of: datetime,
    ) -> tuple[DirectBaselineObservation, ...]:
        """Return finalized player observations available before the target cutoff."""
        return tuple(
            row
            for row in self.players.get((player_id, season), ())
            if _available_before(row, game_start, available_as_of)
        )

    def season_rows_before(
        self,
        season: int,
        game_start: datetime,
        available_as_of: datetime,
    ) -> tuple[DirectBaselineObservation, ...]:
        """Return league observations available before the target cutoff."""
        index = self.seasons.get(season)
        if index is None:
            return ()
        count = bisect_left(index.starts, game_start)
        return tuple(
            row for row in index.rows[:count] if _available_before(row, game_start, available_as_of)
        )

    def season_weighted_mean(
        self,
        season: int,
        game_start: datetime,
        available_as_of: datetime,
    ) -> float | None:
        """Return the recency-weighted mean from prior league outcomes."""
        index = self.seasons.get(season)
        if index is None:
            return None
        count = bisect_left(index.starts, game_start)
        if not count:
            return None
        global_count = bisect_left(self.starts, game_start)
        if self._all_finalized_by(global_count, available_as_of):
            return index.cumulative_weighted_scores[count - 1] / index.cumulative_weights[count - 1]
        eligible = tuple(
            row for row in index.rows[:count] if _available_before(row, game_start, available_as_of)
        )
        if not eligible:
            return None
        origin = eligible[0].game_start
        weighted_score = 0.0
        total_weight = 0.0
        for row in eligible:
            age = (row.game_start - origin).total_seconds() / 86400
            weight = exp(log(2) * age / self.half_life_days)
            weighted_score += calculate_fantasy_points(row.box_score, self.scoring_policy) * weight
            total_weight += weight
        return weighted_score / total_weight

    def fingerprint_before(self, game_start: datetime, available_as_of: datetime) -> str:
        """Return the rolling identity of observations available by cutoff."""
        count = bisect_left(self.starts, game_start)
        if not count:
            return "empty"
        if self._all_finalized_by(count, available_as_of):
            return self.prefix_fingerprints[count - 1]
        fingerprint = ""
        for row in self.rows[:count]:
            if not _available_before(row, game_start, available_as_of):
                continue
            fingerprint = hashlib.sha256(
                f"{fingerprint}:{_row_fingerprint(row)}".encode()
            ).hexdigest()
        return fingerprint or "empty"

    def _all_finalized_by(self, count: int, available_as_of: datetime) -> bool:
        """Return whether prefix aggregates already exclude unavailable explicit outcomes."""
        if not count:
            return True
        latest = self.prefix_latest_finalization[count - 1]
        return latest is None or latest <= available_as_of

    def _reset(self) -> None:
        """Clear direct-specific state before replaying an incompatible sequence."""
        self.processed_count = 0
        self.rows.clear()
        self.starts.clear()
        self.prefix_fingerprints.clear()
        self.prefix_latest_finalization.clear()
        self.seasons.clear()
        self.players.clear()
        self.rows_by_key.clear()
        self.duplicate_keys.clear()


@lru_cache(maxsize=128)
def _source_versions_digest(source_versions: tuple[DatasetSourceVersion, ...]) -> str:
    """Memoize the pure source-identity digest; eviction only recomputes identical values."""
    encoded = json.dumps(
        [(source.provider, source.schema_version, source.source_ids) for source in source_versions],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pregame_input_version(
    request: PregameProjectionRequest,
    policy: ScoringPolicy,
    *,
    history_fingerprint: str,
) -> str:
    """Build the v5 direct-projection input identity without target outcomes."""
    payload = {
        "dataset_version": request.dataset_version,
        "feature_schema_version": request.feature_schema_version,
        "player_id": request.player_id,
        "history_player_id": request.history_player_id,
        "game_id": request.game_id,
        "game_start": request.game_start.isoformat(),
        "available_as_of": request.available_as_of.isoformat(),
        "source_versions_digest": _source_versions_digest(request.source_versions),
        "scoring_policy_version": policy.version,
        "history_fingerprint": history_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"projection-input-v5-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _validate_timestamp(value: datetime, field: str) -> None:
    """Require timezone-aware timestamps at the projection-history boundary."""
    if value.tzinfo is None:
        raise ProjectionBaselineError(f"{field} must be timezone-aware")


def _history_sort_key(row: DirectBaselineObservation) -> tuple[datetime, str, str]:
    """Order observations deterministically within a chronological history."""
    return row.game_start, row.game_id, row.player_id


def _history_fingerprint(rows: Sequence[DirectBaselineObservation]) -> str:
    """Fold canonical observation identities into one ordered history digest."""
    fingerprint = "empty"
    for row in rows:
        fingerprint = hashlib.sha256(f"{fingerprint}:{_row_fingerprint(row)}".encode()).hexdigest()
    return fingerprint


def _available_before(
    row: DirectBaselineObservation,
    game_start: datetime,
    available_as_of: datetime,
) -> bool:
    """Return whether one prior observation is usable for a target projection."""
    if row.game_start >= game_start:
        return False
    if row.outcome_finalized_at is None:
        return True
    return row.outcome_finalized_at <= available_as_of


def _same_historical_boundary(
    committed: HistoricalFeatureRow,
    incoming: HistoricalFeatureRow,
) -> bool:
    """Compare reconstructed boundary rows through their compact direct identity."""
    return _row_fingerprint(
        DirectBaselineObservation.from_historical_row(committed)
    ) == _row_fingerprint(DirectBaselineObservation.from_historical_row(incoming))


@lru_cache(maxsize=131072)
def _row_fingerprint(row: DirectBaselineObservation) -> str:
    """Fingerprint every compact outcome field that can alter a direct projection."""
    payload = {
        "game_id": row.game_id,
        "player_id": row.player_id,
        "game_start": row.game_start.isoformat(),
        "target_minutes": row.minutes,
        "target_started": row.started,
        "target_did_play": not row.did_not_play,
        "outcome_finalized_at": (
            row.outcome_finalized_at.isoformat() if row.outcome_finalized_at is not None else None
        ),
        "target_box_score": (
            row.box_score.points,
            row.box_score.rebounds,
            row.box_score.assists,
            row.box_score.steals,
            row.box_score.blocks,
            row.box_score.turnovers,
            row.box_score.three_pointers_made,
            row.box_score.technical_fouls,
            row.box_score.flagrant_fouls,
        ),
        "source_version": row.source_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = (
    "DirectBaselineObservation",
    "PregameProjectionRequest",
    "ProjectionBaselineError",
    "compact_direct_baseline_observations",
)
