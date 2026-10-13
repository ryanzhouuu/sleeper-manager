"""Bounded async batches preserve full-model behavior, archive gaps, and game status effects."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.persistence.forecast_sqlite import AsyncSQLiteForecastArchiveRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.projections.hybrid_runtime_codec import HybridHistoryManifest
from sleeper_manager.projections.hybrid_runtime_index import build_hybrid_runtime_index
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    HybridHistory,
    ParticipationOpportunity,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import failed_capture
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, game, history
from tests.sleeper_manager.projections.test_hybrid_model import TARGET
from tests.sleeper_manager.projections.test_hybrid_provider import capture
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY


class HistoryStore:
    def __init__(self, data=None) -> None:  # type: ignore[no-untyped-def]
        self.data = data or history()
        self.reads: list[str] = []
        self.index = build_hybrid_runtime_index(
            self.data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
        )

    async def load_history(self, version, **kwargs):  # type: ignore[no-untyped-def]
        manifest = HybridHistoryManifest(version, self.data.dataset_version, CUTOFF, "index", ())
        return manifest, self.index

    async def load_player_games(self, manifest, player):  # type: ignore[no-untyped-def]
        self.reads.append(player)
        return tuple(row for row in self.data.observations if row.player_id == player)


def test_batch_shares_selected_outcome_reads_and_preserves_full_population() -> None:
    rows = tuple(
        game(player, i, points=10 + int(player.split("-")[-1]) if player != "target" else 20)
        for player in ("target", *(f"donor-{i}" for i in range(40)))
        for i in range(30)
    )
    data = HybridHistory(
        "large-population",
        rows,
        tuple(
            ParticipationOpportunity(
                row.player_id, row.game_id, row.game_start, row.finalized_at, True, "census"
            )
            for row in rows
        ),
    )
    store = HistoryStore(data)
    targets = (TARGET, replace(TARGET, game_id="later", game_start=TIPOFF + timedelta(days=1)))
    actual = asyncio.run(
        HybridRuntimeProjectionProvider(store, "version").project_batch(
            targets, scoring_policy=POLICY, decision_time=CUTOFF
        )
    )
    expected = HybridProjectionProvider(data).project_batch(
        targets, scoring_policy=POLICY, decision_time=CUTOFF
    )
    assert len(store.reads) <= 21 < len(store.index.profiles)
    assert len(store.reads) == len(set(store.reads))
    assert actual == expected


def test_empty_archive_falls_back_and_confirmed_absence_affects_only_its_game(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = AsyncSQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    asyncio.run(archive.initialize())
    store = HistoryStore()
    provider = HybridRuntimeProjectionProvider(store, "version", archive=archive)
    other = replace(TARGET, game_id="later", game_start=TIPOFF + timedelta(days=1))
    report = GameAvailability(
        TARGET.game_id,
        CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "official:hash",
        True,
    )
    result = asyncio.run(
        provider.project_live_batch(
            (TARGET, other),
            scoring_policy=POLICY,
            decision_time=CUTOFF,
            availability={(TARGET.sleeper_player_id, TARGET.game_id): report},
        )
    )
    assert result[0].distribution.expected_value == 0
    assert result[1].distribution.expected_value > 0
    assert all(row.source == "internal" and row.external_rejection for row in provider.results)
    assert provider.results[0].participation.probability == 0
    assert provider.results[1].participation.fallback == "missing_game_availability"


def test_async_archive_retains_success_and_a_newer_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = AsyncSQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    asyncio.run(archive.initialize())
    asyncio.run(archive.save_capture(capture("fresh", CUTOFF - timedelta(minutes=1))))
    asyncio.run(archive.save_capture(failed_capture(receipt_id="failed", persisted_at=CUTOFF)))
    result = asyncio.run(
        HybridRuntimeProjectionProvider(HistoryStore(), "version", archive=archive).project_batch(
            (TARGET,), scoring_policy=POLICY, decision_time=CUTOFF
        )
    )[0]
    assert result.source == "external"
    assert result.forecast.newer_attempt_receipt_id == "failed"
    assert result.forecast.provenance.receipt_id == "fresh"


def test_missing_partition_rows_cannot_silently_change_a_verified_profile() -> None:
    class MissingRows(HistoryStore):
        async def load_player_games(self, manifest, player):  # type: ignore[no-untyped-def]
            return ()

    with pytest.raises(HybridProjectionError, match="partition_profile_mismatch"):
        asyncio.run(
            HybridRuntimeProjectionProvider(MissingRows(), "version").project_batch(
                (TARGET,), scoring_policy=POLICY, decision_time=CUTOFF
            )
        )
