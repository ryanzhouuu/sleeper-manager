"""Compact census counts reproduce every status-conditioned participation diagnostic."""

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_participation_counts import ParticipationCounts
from sleeper_manager.projections.hybrid_types import GameAvailability
from tests.sleeper_manager.projections.hybrid_support import CUTOFF
from tests.sleeper_manager.projections.test_hybrid_participation import estimator, opportunity


@pytest.mark.parametrize(
    "status",
    [
        None,
        AvailabilityObservation.REPORTED,
        AvailabilityObservation.MISSING_REPORT,
        AvailabilityObservation.NOT_LISTED,
    ],
)
@pytest.mark.parametrize("role", [False, True])
def test_indexed_counts_preserve_complete_estimate(status, role: bool) -> None:  # type: ignore[no-untyped-def]
    donors = tuple(f"donor-{i}" for i in range(5))
    rows = tuple(opportunity(player, i, i % 2 == 0, status) for player in donors for i in range(8))
    rows += (opportunity("target", 0, True, status), opportunity("bench", 1, False))
    original = estimator(rows)
    indexed = HybridParticipation.from_counts(
        original.counts, cutoff=CUTOFF, config=HybridProjectionConfig()
    )
    report = (
        None
        if status is None
        else GameAvailability("next", CUTOFF, AvailabilityStatus.OUT, status, "report-v1")
    )
    kwargs = {"game_id": "next", "availability": report, "donor_player_ids": donors if role else ()}
    assert original.estimate("target", **kwargs) == indexed.estimate("target", **kwargs)
    assert indexed.opportunities == ()


@pytest.mark.parametrize(
    "counts",
    [
        {"played": 2, "total": 1},
        {"played": True, "total": 1},
        {"played": 0, "total": 2, "buckets": (("reported:out", 1, 1),)},
        {"played": 1, "total": 2, "buckets": (("reported:out", 0, 2),)},
        {"played": 1, "total": 2, "buckets": (("reported:out", 0, 1), ("reported:out", 0, 1))},
    ],
)
def test_invalid_persisted_counts_are_rejected(counts) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(HybridProjectionError):
        ParticipationCounts(**counts)
