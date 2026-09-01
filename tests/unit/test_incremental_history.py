"""Verify shared chronological-prefix synchronization independently of model math."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.projections.incremental_history import (
    IncrementalHistory,
    IncrementalHistoryError,
    visible_prior_count,
)


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """Minimal immutable row carrying chronology and boundary content."""

    key: str
    started_at: datetime


class PointInTimeRows(Sequence[HistoryRow]):
    """Expose legitimate history followed by one uncommitted target row."""

    def __init__(self, rows: tuple[HistoryRow, ...], prior_count: int) -> None:
        self._rows = rows
        self.prior_count = prior_count

    def __len__(self) -> int:
        """Include the target in the visible sequence length."""
        return self.prior_count + 1

    def __getitem__(self, index: int) -> HistoryRow:
        """Return one prior row or the current target from the backing sequence."""
        return self._rows[index]


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def history() -> IncrementalHistory[HistoryRow]:
    """Build a synchronizer whose replacement check uses stable row content."""
    return IncrementalHistory(
        timestamp=lambda row: row.started_at,
        same_boundary=lambda left, right: left == right,
    )


def test_synchronize_commits_only_new_prior_rows_and_excludes_target() -> None:
    """Advance a growing view without recommitting rows or committing its target."""
    rows = tuple(HistoryRow(str(index), BASE + timedelta(days=index)) for index in range(4))
    index = history()
    committed: list[str] = []

    first_limit = index.synchronize(
        PointInTimeRows(rows, 1), commit=lambda row: committed.append(row.key), reset=lambda: None
    )
    second_limit = index.synchronize(
        PointInTimeRows(rows, 3), commit=lambda row: committed.append(row.key), reset=lambda: None
    )

    assert (first_limit, second_limit) == (1, 3)
    assert committed == ["0", "1", "2"]
    assert tuple(row.key for row in index.rows) == ("0", "1", "2")


def test_synchronize_rejects_smaller_confirmed_prefix() -> None:
    """Fail closed when an already-confirmed sequence moves backward."""
    rows = tuple(HistoryRow(str(index), BASE + timedelta(days=index)) for index in range(4))
    index = history()
    index.synchronize(PointInTimeRows(rows, 3), commit=lambda row: None, reset=lambda: None)

    with pytest.raises(IncrementalHistoryError, match="regression"):
        index.synchronize(PointInTimeRows(rows, 1), commit=lambda row: None, reset=lambda: None)


def test_synchronize_resets_incompatible_sequence_and_replays_visible_prefix() -> None:
    """Discard model state when a same-key sequence has a different boundary row."""
    original = tuple(HistoryRow(str(index), BASE + timedelta(days=index)) for index in range(3))
    replacement = (
        HistoryRow("other", BASE),
        HistoryRow("replacement", BASE + timedelta(days=1)),
        HistoryRow("target", BASE + timedelta(days=2)),
    )
    index = history()
    committed: list[str] = []
    resets = 0

    def reset() -> None:
        """Track reset calls and clear the model-owned fixture state."""
        nonlocal resets
        resets += 1
        committed.clear()

    index.synchronize(
        PointInTimeRows(original, 2), commit=lambda row: committed.append(row.key), reset=reset
    )
    index.synchronize(
        PointInTimeRows(replacement, 2), commit=lambda row: committed.append(row.key), reset=reset
    )

    assert resets == 1
    assert committed == ["other", "replacement"]


def test_synchronize_accepts_content_equivalent_reconstructed_boundary() -> None:
    """Continue without resetting when reconstructed rows match boundary content."""
    original = tuple(HistoryRow(str(index), BASE + timedelta(days=index)) for index in range(3))
    reconstructed = tuple(HistoryRow(row.key, row.started_at) for row in original) + (
        HistoryRow("3", BASE + timedelta(days=3)),
    )
    index = history()
    committed: list[str] = []
    resets: list[None] = []
    index.synchronize(
        PointInTimeRows(original, 2),
        commit=lambda row: committed.append(row.key),
        reset=lambda: None,
    )
    index.synchronize(
        PointInTimeRows(reconstructed, 3),
        commit=lambda row: committed.append(row.key),
        reset=lambda: resets.append(None),
    )

    assert committed == ["0", "1", "2"]
    assert not resets


def test_synchronize_rejects_unordered_addition_and_invalid_prior_count() -> None:
    """Validate chronology and point-in-time boundary metadata before reuse."""
    ordered = HistoryRow("ordered", BASE + timedelta(days=1))
    earlier = HistoryRow("earlier", BASE)
    index = history()

    with pytest.raises(IncrementalHistoryError, match="chronologically ordered"):
        index.synchronize((ordered, earlier), commit=lambda row: None, reset=lambda: None)
    with pytest.raises(IncrementalHistoryError, match="between zero"):
        visible_prior_count(PointInTimeRows((ordered,), -1))
