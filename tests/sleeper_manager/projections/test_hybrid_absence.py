"""A verified absence needs no conditional played-stat forecast, even across history gaps."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.projections.hybrid_types import GameAvailability, HybridHistory
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, history
from tests.sleeper_manager.projections.test_hybrid_model import POLICY, TARGET, batch
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY as RUNTIME_POLICY
from tests.sleeper_manager.projections.test_hybrid_runtime_provider import HistoryStore


def missing_stats(kind: str) -> HybridHistory:
    if kind == "missing":
        return HybridHistory("empty", (), ())
    data = history()
    return replace(
        data,
        observations=tuple(
            replace(row, verified_fields=frozenset({"points"})) for row in data.observations
        ),
    )


def absence() -> GameAvailability:
    return GameAvailability(
        TARGET.game_id,
        CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "verified-official",
        True,
    )


@pytest.mark.parametrize("kind", ["missing", "unverified"])
def test_confirmed_absence_is_zero_and_retains_conditional_projection_gap(kind: str) -> None:
    result = batch(missing_stats(kind)).project(TARGET, availability=absence())
    assert result.projection is not None
    assert result.projection.distribution.expected_value == 0
    assert result.projection.scoring_policy_version == POLICY.version
    assert result.failure is None
    assert result.source == "confirmed_absence"
    assert result.conditional_projection_gap in {"missing_stat_history", "unverified_player_stats"}
    assert any(reason.code == "conditional_projection_gap" for reason in result.projection.reasons)


@pytest.mark.parametrize("kind", ["stale", "future", "other_game", "available"])
def test_inapplicable_or_withdrawn_absence_cannot_bypass_missing_stats(kind: str) -> None:
    report = absence()
    if kind in {"stale", "future"}:
        report = replace(
            report, observed_at=CUTOFF + timedelta(minutes=1 if kind == "future" else -16)
        )
    elif kind == "other_game":
        report = replace(report, game_id="other")
    else:
        report = replace(report, status=AvailabilityStatus.AVAILABLE, confirmed_absence=False)
    result = batch(missing_stats("missing")).project(TARGET, availability=report)
    assert result.projection is None
    assert result.failure == "missing_stat_history"


def test_indexed_and_full_history_absences_match_with_no_stat_or_census_rows() -> None:
    data = missing_stats("missing")
    reports = {(TARGET.sleeper_player_id, TARGET.game_id): absence()}
    actual = asyncio.run(
        HybridRuntimeProjectionProvider(HistoryStore(data), "version").project_batch(
            (TARGET,), scoring_policy=RUNTIME_POLICY, decision_time=CUTOFF, availability=reports
        )
    )
    expected = HybridProjectionProvider(data).project_batch(
        (TARGET,), scoring_policy=RUNTIME_POLICY, decision_time=CUTOFF, availability=reports
    )
    assert actual == expected
    assert actual[0].projection is not None
