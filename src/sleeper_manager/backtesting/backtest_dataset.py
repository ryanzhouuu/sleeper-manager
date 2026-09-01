"""Prepare immutable historical datasets for one or more backtest windows.

This module owns dataset validation, chronological indexing, warmup bookkeeping, and
point-in-time target views. Model execution consumes these prepared records without
revalidating or resorting the complete dataset for every fold.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import islice
from typing import overload

from sleeper_manager.backtesting.models import BacktestConfig, BacktestError, TargetSkip
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


@dataclass(frozen=True, slots=True)
class PreparedBacktestWindow:
    """Freeze one configuration's target and warmup-skip selection."""

    config: BacktestConfig
    start_index: int
    stop_index: int
    targets: tuple[HistoricalFeatureRow, ...]
    target_skips: tuple[TargetSkip, ...]


@dataclass(frozen=True, slots=True)
class PreparedBacktestDataset:
    """Hold validated chronological invariants shared by backtest windows."""

    dataset: HistoricalFeatureDataset
    rows: tuple[HistoricalFeatureRow, ...]
    game_starts: tuple[datetime, ...]
    prior_same_season_games: tuple[int, ...]

    def prepare_window(self, config: BacktestConfig) -> PreparedBacktestWindow:
        """Select an inclusive time window without rescanning rows outside it."""
        start_index = (
            0 if config.start_at is None else bisect_left(self.game_starts, config.start_at)
        )
        stop_index = (
            len(self.rows)
            if config.end_at is None
            else bisect_right(self.game_starts, config.end_at)
        )
        targets: list[HistoricalFeatureRow] = []
        target_skips: list[TargetSkip] = []
        for position in range(start_index, stop_index):
            row = self.rows[position]
            prior_games = self.prior_same_season_games[position]
            if prior_games < config.min_prior_games:
                target_skips.append(
                    TargetSkip(
                        player_id=row.player_id,
                        game_id=row.game_id,
                        game_start=row.game_start,
                        reason=(
                            f"Warmup requires {config.min_prior_games} prior same-season games; "
                            f"found {prior_games}."
                        ),
                    )
                )
            else:
                targets.append(row)
        return PreparedBacktestWindow(
            config=config,
            start_index=start_index,
            stop_index=stop_index,
            targets=tuple(targets),
            target_skips=tuple(target_skips),
        )

    def point_in_time_dataset(self, target: HistoricalFeatureRow) -> HistoricalFeatureDataset:
        """Expose only rows strictly before tipoff plus one outcome-sanitized target."""
        prior_count = bisect_left(self.game_starts, target.game_start)
        sanitized_target = replace(
            target,
            target_minutes=None,
            target_started=False,
            target_did_play=False,
            target_box_score=BoxScoreLine(),
            target_line_points=0,
            target_line_rebounds=0,
            target_line_assists=0,
            target_line_steals=0,
            target_line_blocks=0,
            target_line_turnovers=0,
        )
        return replace(
            self.dataset,
            rows=_PointInTimeRows(self.rows, prior_count, sanitized_target),
        )


def prepare_backtest_dataset(dataset: HistoricalFeatureDataset) -> PreparedBacktestDataset:
    """Validate and snapshot invariants reused by every requested backtest window."""
    _validate_dataset(dataset, dataset.rows)
    rows = tuple(sorted(dataset.rows, key=lambda row: (row.game_start, row.game_id, row.player_id)))
    return PreparedBacktestDataset(
        dataset=replace(dataset, rows=rows),
        rows=rows,
        game_starts=tuple(row.game_start for row in rows),
        prior_same_season_games=_prior_same_season_game_counts(rows),
    )


def _validate_dataset(
    dataset: HistoricalFeatureDataset, rows: Sequence[HistoricalFeatureRow]
) -> None:
    """Reject dataset identities or temporal evidence unsafe for point-in-time replay."""
    if dataset.generated_at.tzinfo is None:
        raise BacktestError("Historical dataset generated_at must be timezone-aware")
    keys: set[tuple[str, str]] = set()
    for row in rows:
        key = row.player_id, row.game_id
        if key in keys:
            raise BacktestError(f"Duplicate historical feature row for {key!r}")
        keys.add(key)
        if row.game_start.tzinfo is None or row.available_as_of.tzinfo is None:
            raise BacktestError("Historical feature timestamps must be timezone-aware")
        if row.available_as_of > row.game_start:
            raise BacktestError(f"Feature row {key!r} is available after game start")


def _prior_same_season_game_counts(
    rows: Sequence[HistoricalFeatureRow],
) -> tuple[int, ...]:
    """Count strict-prior player-season rows while excluding the current tipoff batch."""
    counts: list[int] = []
    committed: dict[tuple[str, int], int] = {}
    pending_game_time: datetime | None = None
    pending: dict[tuple[str, int], int] = {}
    for row in rows:
        if pending_game_time is not None and row.game_start != pending_game_time:
            for key, count in pending.items():
                committed[key] = committed.get(key, 0) + count
            pending.clear()
        pending_game_time = row.game_start
        key = row.player_id, nba_season_start_year(row.game_start)
        counts.append(committed.get(key, 0))
        pending[key] = pending.get(key, 0) + 1
    return tuple(counts)


class _PointInTimeRows(Sequence[HistoricalFeatureRow]):
    """Present a stable prior prefix and one sanitized target as a sequence."""

    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self._prior_count = prior_count
        self._target = target

    def __len__(self) -> int:
        return self._prior_count + 1

    @property
    def prior_count(self) -> int:
        """Return the number of legitimate historical rows before the target."""
        return self._prior_count

    def __iter__(self) -> Iterator[HistoricalFeatureRow]:
        """Yield the strict prior prefix followed by the sanitized target."""
        yield from islice(self._rows, self._prior_count)
        yield self._target

    @overload
    def __getitem__(self, index: int) -> HistoricalFeatureRow: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[HistoricalFeatureRow, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> HistoricalFeatureRow | tuple[HistoricalFeatureRow, ...]:
        """Resolve indices without materializing the visible prefix."""
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return tuple(self[position] for position in range(start, stop, step))
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        if normalized == self._prior_count:
            return self._target
        return self._rows[normalized]
