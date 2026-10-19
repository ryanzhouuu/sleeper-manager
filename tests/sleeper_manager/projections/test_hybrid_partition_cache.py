"""Cache limits apply to retained rows and empty entries without changing immutable partitions."""

import pytest

from sleeper_manager.projections.hybrid_partition_cache import HybridPartitionCache
from tests.sleeper_manager.projections.hybrid_support import game


def test_row_budget_evicts_least_recently_used_partition() -> None:
    cache = HybridPartitionCache(maximum_rows=4)
    rows = tuple(game("donor", i) for i in range(2))
    cache.remember("first", rows)
    cache.remember("second", rows)
    assert cache.get("first") is rows
    cache.remember("third", rows)
    assert cache.get("second") is None
    assert cache.get("first") is rows
    assert cache.get("third") is rows


def test_oversized_partition_does_not_flush_reusable_donors() -> None:
    cache = HybridPartitionCache(maximum_rows=2)
    rows = (game("donor", 0),)
    cache.remember("donor", rows)
    cache.remember("oversized", tuple(game("large", i) for i in range(3)))
    assert cache.get("oversized") is None
    assert cache.get("donor") is rows


def test_replacement_releases_previous_rows_from_budget() -> None:
    cache = HybridPartitionCache(maximum_rows=2)
    rows = tuple(game("donor", i) for i in range(2))
    cache.remember("first", rows)
    cache.remember("first", rows[:1])
    cache.remember("second", rows[:1])
    assert cache.get("first") == rows[:1]
    assert cache.get("second") == rows[:1]


def test_empty_partition_is_cached_but_entry_count_is_bounded() -> None:
    cache = HybridPartitionCache(maximum_partitions=2)
    for player in ("first", "second", "third"):
        cache.remember(player, ())
    assert cache.get("first") is None
    assert cache.get("second") == ()
    assert cache.get("third") == ()


@pytest.mark.parametrize("bounds", [{"maximum_rows": 0}, {"maximum_partitions": 0}])
def test_invalid_cache_bounds_are_rejected(bounds: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        HybridPartitionCache(**bounds)
