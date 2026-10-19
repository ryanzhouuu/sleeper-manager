"""Share verified donor outcomes across players without keeping them across live batches."""

import asyncio
from collections import Counter
from dataclasses import replace

from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from tests.sleeper_manager.projections.hybrid_support import CUTOFF
from tests.sleeper_manager.projections.test_hybrid_model import TARGET
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY
from tests.sleeper_manager.projections.test_hybrid_runtime_provider import HistoryStore


def test_shared_donor_partitions_preserve_results_and_target_order() -> None:
    store = HistoryStore()
    other = replace(TARGET, sleeper_player_id="other", provider_player_id="donor-0")
    targets = (TARGET, other)
    expected = HybridProjectionProvider(store.data).project_batch(
        targets, scoring_policy=POLICY, decision_time=CUTOFF
    )
    actual = asyncio.run(
        HybridRuntimeProjectionProvider(store, "version").project_batch(
            targets, scoring_policy=POLICY, decision_time=CUTOFF
        )
    )
    assert actual == expected
    assert len(store.reads) == len(set(store.reads))


def test_partition_reuse_never_skips_reads_in_a_new_batch() -> None:
    store = HistoryStore()
    provider = HybridRuntimeProjectionProvider(store, "version")
    other = replace(TARGET, sleeper_player_id="other", provider_player_id="donor-0")
    for _ in range(2):
        asyncio.run(
            provider.project_batch((TARGET, other), scoring_policy=POLICY, decision_time=CUTOFF)
        )
    assert set(Counter(store.reads).values()) == {2}
