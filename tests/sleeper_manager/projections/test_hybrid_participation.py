from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    HybridHistory,
    ParticipationOpportunity,
)
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF


def opportunity(
    player: str, index: int, played: bool, bucket: AvailabilityObservation | None = None
) -> ParticipationOpportunity:
    start = CUTOFF - timedelta(days=index + 1)
    availability = (
        GameAvailability(
            str(index), start - timedelta(minutes=10), AvailabilityStatus.OUT, bucket, "report-v1"
        )
        if bucket is not None
        else None
    )
    return ParticipationOpportunity(
        player, str(index), start, start + timedelta(hours=3), played, "census-v1", availability
    )


def estimator(rows: tuple[ParticipationOpportunity, ...]) -> HybridParticipation:
    return HybridParticipation(
        HybridHistory("v1", (), rows),
        cutoff=CUTOFF,
        game_start=TIPOFF,
        config=HybridProjectionConfig(),
    )


def test_counts_include_dnp_and_pool_other_players_without_box_scores() -> None:
    rows = (opportunity("p", 0, False),) + tuple(opportunity("other", i, i < 8) for i in range(10))
    result = estimator(rows).estimate("p", game_id="next")
    assert result.probability == pytest.approx(16 / 21)
    assert (result.own_count, result.pooled_count) == (1, 10)
    assert result.fallback == "missing_game_availability"
    assert estimator(rows).estimate("new", game_id="next").probability == pytest.approx(8 / 11)


def test_out_status_uses_observed_bucket_rates_and_twenty_opportunity_shrinkage() -> None:
    rows = tuple(
        opportunity("other", i, i < 4, AvailabilityObservation.REPORTED) for i in range(10)
    )
    rows += (opportunity("p", 0, False, AvailabilityObservation.REPORTED),)
    report = GameAvailability(
        "next", CUTOFF, AvailabilityStatus.OUT, AvailabilityObservation.REPORTED, "v1"
    )
    result = estimator(rows).estimate("p", game_id="next", availability=report)
    assert result.probability == pytest.approx((20 * (4 + 20 * 0.4) / 30) / 21)
    assert result.bucket == "reported:out"
    assert result.pooled_bucket_count == 10
    assert result.fallback is None


@pytest.mark.parametrize("gap", ["missing", "other", "future", "stale"])
def test_nonapplicable_injury_reports_do_not_carry_to_future_games(gap: str) -> None:
    report = GameAvailability(
        "other" if gap == "other" else "next",
        CUTOFF + timedelta(minutes=1)
        if gap == "future"
        else CUTOFF - timedelta(minutes=16)
        if gap == "stale"
        else CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "v1",
    )
    result = estimator((opportunity("other", 0, True),)).estimate(
        "p", game_id="next", availability=None if gap == "missing" else report
    )
    assert result.probability == result.history_probability == 1
    assert result.availability is None
    assert result.fallback is not None


def test_report_coverage_groups_remain_distinct() -> None:
    rows = (
        opportunity("other", 0, False, AvailabilityObservation.MISSING_REPORT),
        opportunity("other", 1, True, AvailabilityObservation.NOT_LISTED),
    )
    base = GameAvailability(
        "next", CUTOFF, AvailabilityStatus.UNKNOWN, AvailabilityObservation.MISSING_REPORT, "v1"
    )
    missing = estimator(rows).estimate("new", game_id="next", availability=base)
    unlisted = estimator(rows).estimate(
        "new",
        game_id="next",
        availability=replace(base, observation=AvailabilityObservation.NOT_LISTED),
    )
    assert missing.probability < unlisted.probability
    assert missing.bucket != unlisted.bucket


def test_future_finalization_and_old_seasons_cannot_change_estimate() -> None:
    row = opportunity("other", 0, True)
    future = replace(opportunity("other", 1, False), finalized_at=CUTOFF + timedelta(seconds=1))
    old = replace(
        opportunity("other", 2, False),
        game_start=CUTOFF.replace(year=2023),
        finalized_at=CUTOFF.replace(year=2023) + timedelta(hours=3),
    )
    assert estimator((row,)).estimate("p", game_id="next") == estimator(
        (row, future, old)
    ).estimate("p", game_id="next")


def test_own_games_without_a_pool_are_an_explicit_gap() -> None:
    with pytest.raises(HybridProjectionError, match="missing_pooled_participation"):
        estimator((opportunity("p", 0, True),)).estimate("p", game_id="next")
