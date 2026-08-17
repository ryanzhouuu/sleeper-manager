from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import astuple
from datetime import datetime
from math import exp, log

from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.opportunity_statistics import _last
from sleeper_manager.projections.opportunity_types import (
    OpportunityModelConfig,
    OpportunityModelError,
    _LeagueProductionPrior,
    _ProductionPrefix,
)


class _HistoricalIndex:
    """Incremental prior-row index for one (dataset, scoring-policy) version pair.

    ``project()`` used to rescan the entire point-in-time row prefix on every call to
    find the target row and to filter player/league prior rows. That is O(n) work per
    target, and a production-sized backtest calls it once per target per model. This
    index instead commits rows once, in the order the caller reveals them, and answers
    later prior-row queries with a bisection over already-sorted per-player and
    league buckets.

    Two input shapes are supported:

    * A growing point-in-time prefix (as produced by the backtest runner), recognized
      by a duck-typed ``prior_count`` attribute giving the exact number of legitimate
      prior rows (the row at that position is the current, still-in-flight target and
      is never committed, so a run of targets sharing one tipoff does not force a
      rebuild).
    * An ordinary, complete `Sequence[HistoricalFeatureRow]`, which is indexed in full
      on first use and then reused as-is for later queries against the same object.

    Both paths share one continuity check: before trusting an incoming sequence as a
    continuation of what is already committed, the row at the last position both views
    can see is compared by identity. A match confirms it is the same underlying
    sequence, at which point a visible prefix that is smaller than what has already
    been committed is a chronological regression and raises explicitly. A mismatch
    means the caller handed over unrelated data under the same version key, which is
    handled by discarding and rebuilding rather than raising.
    """

    def __init__(
        self,
        dataset_version: str,
        scoring_policy: ScoringPolicy,
        recency_half_life_days: float,
    ) -> None:
        self.dataset_version = dataset_version
        self.scoring_policy = scoring_policy
        self.scoring_policy_version = scoring_policy.version
        self.recency_half_life_days = recency_half_life_days
        self._rows: list[HistoricalFeatureRow] = []
        self._by_target: dict[tuple[str, str], HistoricalFeatureRow] = {}
        self._duplicate_targets: set[tuple[str, str]] = set()
        self._player_rows: dict[str, list[HistoricalFeatureRow]] = {}
        self._league_starts: list[datetime] = []
        self._league_score_prefix: list[float] = []
        self._league_minutes_prefix: list[float] = []
        self._league_fingerprints: list[str] = []
        self._league_by_player: dict[str, list[_ProductionPrefix]] = {}
        self._league_origin: datetime | None = None

    def matches(self, dataset_version: str, scoring_policy_version: str) -> bool:
        return (
            self.dataset_version == dataset_version
            and self.scoring_policy_version == scoring_policy_version
        )

    def resolve_target(
        self, rows: Sequence[HistoricalFeatureRow], player_id: str, game_id: str
    ) -> HistoricalFeatureRow:
        limit = self._sync(rows)
        if limit == len(rows) - 1:
            candidate = rows[limit]
            if candidate.player_id == player_id and candidate.game_id == game_id:
                return candidate
        key = (player_id, game_id)
        if key in self._duplicate_targets:
            raise OpportunityModelError("Expected one feature row for player/game, found multiple")
        row = self._by_target.get(key)
        if row is None:
            raise OpportunityModelError("Expected one feature row for player/game, found 0")
        return row

    def player_prior_rows(
        self, player_id: str, cutoff: datetime
    ) -> tuple[HistoricalFeatureRow, ...]:
        rows = self._player_rows.get(player_id)
        if not rows:
            return ()
        count = bisect_left(rows, cutoff, key=lambda candidate: candidate.game_start)
        return tuple(rows[:count])

    def league_production_prior(self, player_id: str, cutoff: datetime) -> _LeagueProductionPrior:
        count = bisect_left(self._league_starts, cutoff)
        if count == 0:
            return _LeagueProductionPrior(None, None, "empty")
        score = self._league_score_prefix[count - 1]
        minutes = self._league_minutes_prefix[count - 1]
        player_score, player_minutes = self._player_production_sums(player_id, cutoff)
        independent_minutes = minutes - player_minutes
        independent_rate = (
            (score - player_score) / independent_minutes if independent_minutes > 0 else None
        )
        shared_rate = score / minutes if minutes > 0 else None
        return _LeagueProductionPrior(
            independent_rate,
            shared_rate,
            self._league_fingerprints[count - 1],
        )

    def _player_production_sums(self, player_id: str, cutoff: datetime) -> tuple[float, float]:
        rows = self._league_by_player.get(player_id)
        if not rows:
            return 0.0, 0.0
        count = bisect_left(rows, cutoff, key=lambda candidate: candidate.game_start)
        if count == 0:
            return 0.0, 0.0
        prefix = rows[count - 1]
        return prefix.score, prefix.minutes

    def _sync(self, rows: Sequence[HistoricalFeatureRow]) -> int:
        limit = _prior_count(rows)
        if limit is None:
            limit = len(rows)
        committed = len(self._rows)
        check_index = min(limit, committed) - 1
        if check_index < 0 or rows[check_index] is self._rows[check_index]:
            if limit < committed:
                raise OpportunityModelError(
                    "Historical index detected a chronological regression: the visible "
                    f"row prefix shrank from {committed} to {limit} rows for what was "
                    "already confirmed to be the same underlying sequence."
                )
        else:
            self._reset()
        self._advance(rows, limit)
        return limit

    def _advance(self, rows: Sequence[HistoricalFeatureRow], limit: int) -> None:
        for index in range(len(self._rows), limit):
            self._commit(rows[index])

    def _commit(self, row: HistoricalFeatureRow) -> None:
        if self._rows and row.game_start < self._rows[-1].game_start:
            raise OpportunityModelError(
                "Historical index requires chronologically ordered rows; encountered "
                f"{row.game_start.isoformat()} after {self._rows[-1].game_start.isoformat()}."
            )
        self._rows.append(row)
        key = (row.player_id, row.game_id)
        if key in self._by_target:
            self._duplicate_targets.add(key)
        else:
            self._by_target[key] = row
        self._player_rows.setdefault(row.player_id, []).append(row)
        if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0:
            self._commit_league_production(row)

    def _commit_league_production(self, row: HistoricalFeatureRow) -> None:
        if self._league_origin is None:
            self._league_origin = row.game_start
        age_days = (row.game_start - self._league_origin).total_seconds() / 86400
        base_weight = exp(log(2) * age_days / self.recency_half_life_days)
        score = calculate_fantasy_points(row.target_box_score, self.scoring_policy) * base_weight
        minutes = (row.target_minutes or 0.0) * base_weight
        self._league_starts.append(row.game_start)
        self._league_score_prefix.append(score + _last(self._league_score_prefix))
        self._league_minutes_prefix.append(minutes + _last(self._league_minutes_prefix))
        prior_fingerprint = self._league_fingerprints[-1] if self._league_fingerprints else ""
        encoded = json.dumps(_input_row(row), separators=(",", ":")).encode()
        fingerprint = hashlib.sha256(prior_fingerprint.encode() + encoded).hexdigest()
        self._league_fingerprints.append(fingerprint)
        player_rows = self._league_by_player.setdefault(row.player_id, [])
        player_score = player_rows[-1].score if player_rows else 0.0
        player_minutes = player_rows[-1].minutes if player_rows else 0.0
        player_rows.append(
            _ProductionPrefix(row.game_start, player_score + score, player_minutes + minutes)
        )

    def _reset(self) -> None:
        self._rows = []
        self._by_target = {}
        self._duplicate_targets = set()
        self._player_rows = {}
        self._league_starts = []
        self._league_score_prefix = []
        self._league_minutes_prefix = []
        self._league_fingerprints = []
        self._league_by_player = {}
        self._league_origin = None


def _prior_count(rows: Sequence[HistoricalFeatureRow]) -> int | None:
    value = getattr(rows, "prior_count", None)
    return value if isinstance(value, int) else None


def _input_version(
    dataset: HistoricalFeatureDataset,
    target: HistoricalFeatureRow,
    prior_rows: Sequence[HistoricalFeatureRow],
    league_prior_fingerprint: str,
    scoring_policy: ScoringPolicy,
    config: OpportunityModelConfig,
) -> str:
    payload = {
        "dataset": dataset.dataset_version,
        "feature_schema": dataset.feature_schema_version,
        "model_config": config.model_version,
        "target": (target.player_id, target.game_id, target.available_as_of.isoformat()),
        "target_environment": (
            target.pace_factor,
            target.opponent_defensive_rating,
            target.league_defensive_rating,
            target.days_rest,
            target.is_back_to_back,
            target.time_zone_change_hours,
        ),
        "player_prior": tuple(_input_row(row) for row in prior_rows),
        "league_prior": league_prior_fingerprint,
        "scoring": scoring_policy.version,
    }
    return "inputs-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _input_row(row: HistoricalFeatureRow) -> tuple[object, ...]:
    return (
        row.player_id,
        row.game_id,
        row.game_start.isoformat(),
        row.target_did_play,
        row.target_minutes,
        astuple(row.target_box_score),
    )
