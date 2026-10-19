"""Reuse conditional fits without sharing game-specific evidence or crossing preparation cutoffs."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.persistence.forecast_sqlite import AsyncSQLiteForecastArchiveRepository
from sleeper_manager.projections.hybrid_fitting import fit_joint_weights
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.projections.hybrid_types import GameAvailability
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF
from tests.sleeper_manager.projections.test_hybrid_model import TARGET, batch, forecast
from tests.sleeper_manager.projections.test_hybrid_provider import capture
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY
from tests.sleeper_manager.projections.test_hybrid_runtime_provider import HistoryStore


@pytest.mark.parametrize("unsupported", [False, True])
def test_identical_fits_preserve_each_games_availability_and_fallback(unsupported: bool) -> None:
    prepared = batch()
    center = prepared.pools.pool("target").means
    read = forecast((100, *center[1:]) if unsupported else center)
    later = replace(TARGET, game_id="later", game_start=TIPOFF + timedelta(days=1))
    absence = GameAvailability(
        TARGET.game_id,
        CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "official:absence",
        True,
    )
    expected = (
        batch().project(TARGET, forecast=read, availability=absence),
        batch().project(later, forecast=read),
    )
    with patch(
        "sleeper_manager.projections.hybrid_model.fit_joint_weights", wraps=fit_joint_weights
    ) as fit:
        actual = (
            prepared.project(TARGET, forecast=read, availability=absence),
            prepared.project(later, forecast=read),
        )
    assert actual == expected
    assert actual[0].projection.distribution.expected_value == 0
    assert actual[1].projection.distribution.expected_value > 0
    assert actual[0].projection.input_version != actual[1].projection.input_version
    assert actual[0].source == ("internal" if unsupported else "external")
    assert fit.call_count == 1


def test_fit_reuse_distinguishes_players_and_changed_forecast_centers() -> None:
    prepared = batch()
    center = prepared.pools.pool("target").means
    read = forecast(center)
    changed = forecast((center[0] + 4, *center[1:]))
    other = replace(TARGET, provider_player_id="donor-0")
    expected = (
        batch().project(TARGET, forecast=read),
        batch().project(TARGET, forecast=changed),
        batch().project(other, forecast=read),
    )
    with patch(
        "sleeper_manager.projections.hybrid_model.fit_joint_weights", wraps=fit_joint_weights
    ) as fit:
        actual = (
            prepared.project(TARGET, forecast=read),
            prepared.project(TARGET, forecast=changed),
            prepared.project(other, forecast=read),
        )
        assert prepared.project(TARGET, forecast=read) == actual[0]
    assert actual == expected
    assert actual[0].joint_weights != actual[1].joint_weights
    assert fit.call_count == 3


def test_same_center_keeps_new_forecast_provenance_without_refitting() -> None:
    prepared = batch()
    read = forecast(prepared.pools.pool("target").means)
    newer = replace(read, newer_attempt_receipt_id="newer-failed")
    with patch(
        "sleeper_manager.projections.hybrid_model.fit_joint_weights", wraps=fit_joint_weights
    ) as fit:
        original = prepared.project(TARGET, forecast=read)
        updated = prepared.project(TARGET, forecast=newer)
    assert updated == batch().project(TARGET, forecast=newer)
    assert updated.forecast == newer
    assert updated.joint_weights == original.joint_weights
    assert updated.projection.input_version != original.projection.input_version
    assert fit.call_count == 1


def test_runtime_fit_reuse_ends_at_each_batch_boundary(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = AsyncSQLiteForecastArchiveRepository(tmp_path / "archive.db")
    asyncio.run(archive.initialize())
    asyncio.run(archive.save_capture(capture("fresh", CUTOFF)))
    provider = HybridRuntimeProjectionProvider(HistoryStore(), "version", archive=archive)
    targets = (TARGET, replace(TARGET, game_id="later", game_start=TIPOFF + timedelta(days=1)))
    with patch(
        "sleeper_manager.projections.hybrid_model.fit_joint_weights", wraps=fit_joint_weights
    ) as fit:
        first = asyncio.run(
            provider.project_batch(targets, scoring_policy=POLICY, decision_time=CUTOFF)
        )
        second = asyncio.run(
            provider.project_batch(
                targets, scoring_policy=POLICY, decision_time=CUTOFF + timedelta(minutes=1)
            )
        )
    assert all(row.source == "external" for row in (*first, *second))
    assert all(row.cutoff == CUTOFF + timedelta(minutes=1) for row in second)
    assert first[0].projection.input_version != second[0].projection.input_version
    assert fit.call_count == 2
