from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sleeper_manager.backtesting.models import BacktestError
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

NaiveProjectionKind = Literal["last_game", "season_average"]


class NaiveProjectionBaseline:
    def __init__(
        self,
        kind: NaiveProjectionKind,
        *,
        percentiles: tuple[int, ...] = (10, 25, 50, 75, 90),
    ) -> None:
        if kind not in ("last_game", "season_average"):
            raise BacktestError(f"Unknown naive projection kind: {kind!r}")
        if not percentiles or tuple(sorted(set(percentiles))) != percentiles:
            raise BacktestError("Naive projection percentiles must be unique and ordered")
        if any(percentile < 0 or percentile > 100 for percentile in percentiles):
            raise BacktestError("Naive projection percentiles must be between zero and 100")
        self.kind = kind
        self.percentiles = percentiles
        self._indexes: dict[str, _NaiveHistoryIndex] = {}

    @property
    def model_version(self) -> str:
        return f"naive-{self.kind.replace('_', '-')}-v1"

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
        index = self._indexes.setdefault(dataset.dataset_version, _NaiveHistoryIndex())
        point_in_time_count = getattr(dataset.rows, "prior_count", None)
        target_is_last = bool(dataset.rows) and dataset.rows[-1] == target
        prior_count = (
            point_in_time_count
            if isinstance(point_in_time_count, int)
            else len(dataset.rows) - int(target_is_last)
        )
        index.extend(dataset.rows, prior_count)
        prior_rows = index.rows_before(
            player_id,
            _season_key(target.game_start),
            target.game_start,
        )
        if not prior_rows:
            raise BacktestError(
                f"No prior same-season observations for {player_id!r} before {game_id!r}"
            )
        prior_scores = tuple(
            calculate_fantasy_points(row.target_box_score, scoring_policy) for row in prior_rows
        )
        observations: tuple[tuple[float, float], ...]
        if self.kind == "last_game":
            latest = max(prior_rows, key=lambda row: (row.game_start, row.game_id))
            observations = (
                (calculate_fantasy_points(latest.target_box_score, scoring_policy), 1.0),
            )
            message = f"Used the latest prior same-season score of {observations[0][0]:.2f}."
        else:
            observations = tuple((score, 1.0) for score in prior_scores)
            message = (
                f"Used an equal-weighted average of {len(prior_scores)} prior same-season scores."
            )
        distribution = ProjectionDistribution.from_weighted_observations(
            observations, percentiles=self.percentiles
        )
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.model_version,
            input_version=_input_version(dataset, target, prior_rows, scoring_policy),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=(ProjectionReason("naive_control", message),),
        )


@dataclass(slots=True)
class _NaiveHistoryIndex:
    processed_count: int = 0
    players: dict[tuple[str, int], list[HistoricalFeatureRow]] = field(default_factory=dict)

    def extend(self, rows: Sequence[HistoricalFeatureRow], prior_count: int) -> None:
        if prior_count <= self.processed_count:
            return
        for row in rows[self.processed_count : prior_count]:
            self.players.setdefault((row.player_id, _season_key(row.game_start)), []).append(row)
        self.processed_count = prior_count

    def rows_before(
        self,
        player_id: str,
        season: int,
        game_start: datetime,
    ) -> tuple[HistoricalFeatureRow, ...]:
        return tuple(
            row for row in self.players.get((player_id, season), ()) if row.game_start < game_start
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
        raise BacktestError(f"Expected one feature row for player/game, found {len(matches)}")
    return matches[0]


def _season_key(value: datetime) -> int:
    return value.year if value.month >= 10 else value.year - 1


def _input_version(
    dataset: HistoricalFeatureDataset,
    target: HistoricalFeatureRow,
    prior_rows: Iterable[HistoricalFeatureRow],
    policy: ScoringPolicy,
) -> str:
    prior_inputs = [
        {
            "game_id": row.game_id,
            "game_start": row.game_start.isoformat(),
            "score": calculate_fantasy_points(row.target_box_score, policy),
        }
        for row in sorted(prior_rows, key=lambda row: (row.game_start, row.game_id))
    ]
    payload = {
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "player_id": target.player_id,
        "game_id": target.game_id,
        "available_as_of": target.available_as_of.isoformat(),
        "scoring_policy_version": policy.version,
        "prior_inputs": prior_inputs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"naive-input-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"
