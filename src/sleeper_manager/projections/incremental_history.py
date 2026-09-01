"""Synchronize model-owned indexes with chronological point-in-time row prefixes.

This module owns only continuity and advancement. Projection models remain responsible
for target lookup, aggregates, fingerprints, and model-specific error translation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime


class IncrementalHistoryError(ValueError):
    """Report an invalid prefix boundary or chronological history transition."""


class IncrementalHistory[Row]:
    """Commit newly visible rows while detecting regressions and sequence replacement.

    Object identity is the normal continuity fast path. ``same_boundary`` may confirm
    content-equivalent boundary rows when a caller reconstructs an otherwise compatible
    sequence with new objects.
    """

    def __init__(
        self,
        *,
        timestamp: Callable[[Row], datetime],
        same_boundary: Callable[[Row, Row], bool] | None = None,
    ) -> None:
        self._timestamp = timestamp
        self._same_boundary = same_boundary
        self._rows: list[Row] = []

    @property
    def rows(self) -> Sequence[Row]:
        """Expose the committed sequence for model-specific lookup without copying it."""
        return self._rows

    def synchronize(
        self,
        rows: Sequence[Row],
        *,
        commit: Callable[[Row], None],
        reset: Callable[[], None],
    ) -> int:
        """Advance through the visible prior prefix and return its exclusive limit.

        A confirmed sequence may only grow. An incompatible boundary resets the model
        index before the incoming prefix is replayed from the beginning.
        """
        limit = visible_prior_count(rows)
        committed = len(self._rows)
        check_index = min(limit, committed) - 1
        boundary_matches = check_index < 0 or self._boundary_matches(
            self._rows[check_index], rows[check_index]
        )
        if boundary_matches and limit < committed:
            raise IncrementalHistoryError(
                "Historical index detected a chronological regression: the visible "
                f"row prefix shrank from {committed} to {limit} rows for what was "
                "already confirmed to be the same underlying sequence."
            )
        if not boundary_matches:
            reset()
            self._rows.clear()
        self._advance(rows, limit, commit=commit)
        return limit

    def _boundary_matches(self, committed: Row, incoming: Row) -> bool:
        """Use identity first and optional content equivalence only on replacement."""
        return committed is incoming or (
            self._same_boundary is not None and self._same_boundary(committed, incoming)
        )

    def _advance(
        self,
        rows: Sequence[Row],
        limit: int,
        *,
        commit: Callable[[Row], None],
    ) -> None:
        """Validate and commit each newly visible row in source order."""
        for index in range(len(self._rows), limit):
            row = rows[index]
            if self._rows and self._timestamp(row) < self._timestamp(self._rows[-1]):
                raise IncrementalHistoryError(
                    "Historical index requires chronologically ordered rows; encountered "
                    f"{self._timestamp(row).isoformat()} after "
                    f"{self._timestamp(self._rows[-1]).isoformat()}."
                )
            commit(row)
            self._rows.append(row)


def visible_prior_count(rows: Sequence[object]) -> int:
    """Return the exclusive legitimate-history limit exposed by a row sequence."""
    value: object = getattr(rows, "prior_count", None)
    if value is None:
        return len(rows)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IncrementalHistoryError("Historical prior_count must be an integer")
    if value < 0 or value > len(rows):
        raise IncrementalHistoryError(
            f"Historical prior_count must be between zero and {len(rows)}, found {value}"
        )
    return value


__all__ = ("IncrementalHistory", "IncrementalHistoryError", "visible_prior_count")
