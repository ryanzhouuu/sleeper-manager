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


def test_role_prior_uses_verified_donor_opportunities_and_excludes_bench_pool() -> None:
    donors = tuple(f"donor-{i}" for i in range(5))
    rows = tuple(opportunity(player, i, True) for i, player in enumerate(donors))
    rows += tuple(opportunity("bench", i, False) for i in range(30))
    result = estimator(rows).estimate("new", game_id="next", donor_player_ids=donors)
    assert result.probability == 1
    assert result.pool_kind == "stat_profile_donors"
    assert result.pooled_count == 5
    assert result.pooled_player_count == 5
    assert result.pool_fallback is None


def test_sparse_donor_census_falls_back_explicitly_to_global_pool() -> None:
    donors = tuple(f"donor-{i}" for i in range(5))
    rows = tuple(opportunity(player, i, True) for i, player in enumerate(donors[:4]))
    rows += (opportunity("bench", 0, False),)
    result = estimator(rows).estimate("new", game_id="next", donor_player_ids=donors)
    assert result.probability == pytest.approx(0.8)
    assert result.pool_kind == "global"
    assert result.pool_fallback == "insufficient_role_census"
    assert result.pooled_count == 5


def test_unfinalized_donor_rows_cannot_satisfy_role_coverage() -> None:
    donors = tuple(f"donor-{i}" for i in range(5))
    rows = tuple(opportunity(player, i, True) for i, player in enumerate(donors[:4]))
    pending = replace(opportunity(donors[4], 9, False), finalized_at=CUTOFF + timedelta(seconds=1))
    result = estimator(rows + (pending,)).estimate("new", game_id="next", donor_player_ids=donors)
    assert result.pool_fallback == "insufficient_role_census"
    assert result.pooled_player_count == 4


def test_confirmed_game_absence_overrides_missing_status_history() -> None:
    report = GameAvailability(
        "next",
        CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "official-report-sha256",
        confirmed_absence=True,
    )
    result = estimator((opportunity("other", 0, True),)).estimate(
        "p", game_id="next", availability=report
    )
    assert result.history_probability == 1
    assert result.probability == 0
    assert result.pooled_bucket_count == 0
    assert result.fallback is None
    assert result.availability == report


@pytest.mark.parametrize("gap", ["other", "future", "stale"])
def test_inapplicable_confirmed_absence_does_not_zero_participation(gap: str) -> None:
    report = GameAvailability(
        "other" if gap == "other" else "next",
        CUTOFF + timedelta(seconds=1)
        if gap == "future"
        else CUTOFF - timedelta(minutes=16)
        if gap == "stale"
        else CUTOFF,
        AvailabilityStatus.OUT,
        AvailabilityObservation.REPORTED,
        "official-v1",
        confirmed_absence=True,
    )
    result = estimator((opportunity("other", 0, True),)).estimate(
        "p", game_id="next", availability=report
    )
    assert result.probability == 1
    assert result.availability is None


@pytest.mark.parametrize("status", [AvailabilityStatus.QUESTIONABLE, AvailabilityStatus.AVAILABLE])
def test_only_reported_out_can_be_a_confirmed_absence(status: AvailabilityStatus) -> None:
    with pytest.raises(HybridProjectionError, match="invalid_confirmed_absence"):
        GameAvailability(
            "next",
            CUTOFF,
            status,
            AvailabilityObservation.REPORTED,
            "official-v1",
            confirmed_absence=True,
        )
