"""Batch-owned LRU for decoded outcomes; active player preparation remains outside its limits."""

from collections import OrderedDict

from sleeper_manager.projections.hybrid_types import JointGameObservation


class HybridPartitionCache:
    """Bound retained rows and entries, including empty partitions, for one pinned history batch."""

    def __init__(self, *, maximum_rows: int = 8192, maximum_partitions: int = 64) -> None:
        if maximum_rows <= 0 or maximum_partitions <= 0:
            raise ValueError("Partition cache bounds must be positive")
        self._maximum_rows = maximum_rows
        self._maximum_partitions = maximum_partitions
        self._partitions: OrderedDict[str, tuple[JointGameObservation, ...]] = OrderedDict()
        self._row_count = 0

    def get(self, player: str) -> tuple[JointGameObservation, ...] | None:
        """Return an immutable partition and retain recently needed donors ahead of older ones."""
        rows = self._partitions.get(player)
        if rows is not None:
            self._partitions.move_to_end(player)
        return rows

    def remember(self, player: str, rows: tuple[JointGameObservation, ...]) -> None:
        """Skip oversized partitions; eviction affects only reuse, never mandatory outcome reads."""
        previous = self._partitions.pop(player, ())
        self._row_count -= len(previous)
        if len(rows) > self._maximum_rows:
            return
        while self._partitions and (
            self._row_count + len(rows) > self._maximum_rows
            or len(self._partitions) >= self._maximum_partitions
        ):
            _, removed = self._partitions.popitem(last=False)
            self._row_count -= len(removed)
        self._partitions[player] = rows
        self._row_count += len(rows)
