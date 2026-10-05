"""Opening-week external priority, independent fallback, scoring, and evidence isolation."""

from dataclasses import replace
from datetime import timedelta
from math import fsum

import pytest

from sleeper_manager.domain.forecast_capture import (
    ForecastCoverage,
    ForecastRetrievalResult,
    ForecastRetrievalStatus,
    ForecastRevisionProvenance,
    NormalizedPlayerForecast,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.projections.hybrid_config import CORE_STATS, HybridProjectionConfig
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch
from sleeper_manager.projections.hybrid_types import HybridHistory, ParticipationOpportunity
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, game, history

TARGET = LiveProjectionTarget("sleeper-target", "next", TIPOFF, "target")
POLICY = ScoringPolicy(
    points=1,
    rebounds=1.2,
    assists=1.5,
    double_double=8,
    triple_double=15,
    technical_foul=-4,
    flagrant_foul=-6,
)


def batch(
    data: HybridHistory | None = None, *, config: HybridProjectionConfig | None = None
) -> HybridProjectionBatch:
    return HybridProjectionBatch(
        data or history(), cutoff=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY, config=config
    )


def forecast(center: tuple[float, ...], *, age: float = 0) -> ForecastRetrievalResult:
    return ForecastRetrievalResult(
        ForecastRetrievalStatus.AVAILABLE,
        TARGET.sleeper_player_id,
        CUTOFF,
        "visible",
        NormalizedPlayerForecast(
            TARGET.sleeper_player_id,
            "sleeper",
            tuple(zip(CORE_STATS, center, strict=True)),
            provider_updated_at=CUTOFF - timedelta(days=90),
        ),
        ForecastRevisionProvenance(
            "receipt",
            "revision",
            season_forecast_source("2026", "regular"),
            "a" * 64,
            "b" * 64,
            CUTOFF - timedelta(hours=age),
        ),
        ForecastCoverage(1, 1, 1),
    )


def test_external_priority_preserves_whole_outcomes_and_bonus_scoring() -> None:
    prepared = batch()
    original = prepared.pools.pool("target")
    center = (original.means[0] + 4,) + original.means[1:]
    result = prepared.project(TARGET, forecast=forecast(center))
    assert result.source == "external"
    assert result.external_rejection is None
    assert result.projection is not None
    assert result.actual_center[0] == pytest.approx(center[0] * 0.9, abs=1e-6)
    lookup = {(row.player_id, row.game_id): row for row in history().observations}
    expected = fsum(
        calculate_fantasy_points(lookup[(p, g)].line, POLICY) * w
        for p, g, w in result.joint_weights
    )
    assert result.projection.distribution.expected_value == pytest.approx(expected, abs=1e-6)
    assert result.projection.scoring_policy_version == POLICY.version
    assert result.effective_sample >= 20
    assert result.maximum_weight <= 0.1


def test_unsupported_external_center_rebuilds_the_internal_distribution() -> None:
    prepared = batch()
    internal = prepared.project(TARGET)
    fallback = prepared.project(TARGET, forecast=forecast((100, 8, 4, 1, 0.5, 1, 1)))
    assert fallback.source == "internal"
    assert fallback.external_rejection == "external_center_outside_support"
    assert fallback.joint_weights == internal.joint_weights
    assert fallback.donor_ids == internal.donor_ids
    assert fallback.projection.distribution == internal.projection.distribution


@pytest.mark.parametrize(
    "case", ["stale", "incomplete", "negative", "incoherent", "season", "dropped", "gap"]
)
def test_external_qualification_returns_a_reason_and_internal_output(case: str) -> None:
    read = forecast(batch().pools.pool("target").means, age=49 if case == "stale" else 48)
    if case in ("incomplete", "negative", "incoherent"):
        stats = read.forecast.stats
        if case == "incomplete":
            stats = tuple((k, v) for k, v in stats if k != "blk")
        else:
            stats = tuple(
                (
                    k,
                    -1
                    if case == "negative" and k == "blk"
                    else 100
                    if case == "incoherent" and k == "tpm"
                    else v,
                )
                for k, v in stats
            )
        read = replace(read, forecast=replace(read.forecast, stats=stats))
    if case == "season":
        read = replace(
            read,
            provenance=replace(read.provenance, source=season_forecast_source("2025", "regular")),
        )
    if case == "dropped":
        read = replace(read, status=ForecastRetrievalStatus.MISSING, forecast=None)
    if case == "gap":
        read = ForecastRetrievalResult(
            ForecastRetrievalStatus.GAP,
            TARGET.sleeper_player_id,
            CUTOFF,
            "failed",
            evidence_receipt_id="failure",
        )
    result = batch().project(TARGET, forecast=read)
    assert result.projection is not None
    assert result.source == "internal"
    assert result.external_rejection is not None


def test_newer_failure_does_not_renew_age_and_provider_timestamp_does_not_expire_row() -> None:
    center = batch().pools.pool("target").means
    fresh = replace(forecast(center, age=48), newer_attempt_receipt_id="newer-failed")
    result = batch().project(TARGET, forecast=fresh)
    assert result.source == "external"
    assert "newer_forecast_attempt_gap" in {reason.code for reason in result.projection.reasons}
    stale = replace(forecast(center, age=49), newer_attempt_receipt_id="newer-failed")
    assert batch().project(TARGET, forecast=stale).external_rejection == "stale_forecast_receipt"


def test_dnp_mass_is_mixed_after_conditional_scoring_without_epsilon_samples() -> None:
    data = history()
    dnp = ParticipationOpportunity(
        "target", "dnp", CUTOFF - timedelta(days=1), CUTOFF - timedelta(hours=12), False, "v1"
    )
    result = batch(replace(data, opportunities=data.opportunities + (dnp,))).project(TARGET)
    assert result.participation.probability == pytest.approx(50 / 51)
    assert result.projection.distribution.weighted_observations[-1] == pytest.approx((0, 1 / 51))
    assert not any(
        score == 0
        for score, _ in batch().project(TARGET).projection.distribution.weighted_observations
    )


def test_no_history_identity_and_source_gaps_are_distinct() -> None:
    new = batch(history(include_player=False)).project(TARGET)
    assert new.history_kind == "no_history"
    assert new.projection is not None
    assert (
        batch().project(replace(TARGET, provider_player_id=None)).failure
        == "unresolved_history_identity"
    )
    data = history()
    uncovered = replace(
        data,
        observations=tuple(
            replace(row, verified_fields=("points",)) if row.player_id == "target" else row
            for row in data.observations
        ),
    )
    assert batch(uncovered).project(TARGET).failure == "unverified_player_stats"
    assert (
        batch(replace(data, opportunities=())).project(TARGET).failure
        == "missing_pooled_participation_opportunities"
    )


def test_future_outcomes_cannot_change_projection_or_input_identity() -> None:
    data = history()
    future = game("future", 0, start=CUTOFF + timedelta(hours=1), points=100)
    poisoned = replace(data, observations=data.observations + (future,))
    assert batch(data).project(TARGET) == batch(poisoned).project(TARGET)
    assert batch(replace(data, observations=tuple(reversed(data.observations)))).project(
        TARGET
    ) == batch(data).project(TARGET)


def test_internal_only_has_separate_model_identity_and_ignores_external_inputs() -> None:
    prepared = batch(config=HybridProjectionConfig(use_external=False))
    result = prepared.project(TARGET, forecast=forecast((100, 8, 4, 1, 0.5, 1, 1)))
    assert result == prepared.project(TARGET)
    assert result.source == "internal"
    assert result.projection.model_version != batch().project(TARGET).projection.model_version


def test_insufficient_internal_support_preserves_the_failed_opportunity() -> None:
    data = history()
    short = replace(data, observations=(data.observations[0],))
    result = batch(short).project(TARGET)
    assert result.target == TARGET
    assert result.failure == "insufficient_effective_sample"
    assert result.projection is None


def test_missing_minutes_cannot_be_presented_as_no_history() -> None:
    data = history()
    missing = replace(
        data,
        observations=tuple(
            replace(row, minutes=None) if row.player_id == "target" else row
            for row in data.observations
        ),
    )
    assert batch(missing).project(TARGET).failure == "unverified_player_stats"


def test_threshold_bonuses_and_fouls_come_from_each_joint_outcome() -> None:
    data = history()
    first = data.observations[0]
    exceptional = replace(
        first,
        line=replace(
            first.line, points=50, rebounds=20, assists=15, technical_fouls=1, flagrant_fouls=2
        ),
    )
    policy = replace(
        POLICY, bonus_40_points=7, bonus_50_points=11, bonus_15_assists=5, bonus_20_rebounds=3
    )
    prepared = HybridProjectionBatch(
        replace(data, observations=(exceptional,) + data.observations[1:]),
        cutoff=CUTOFF,
        game_start=TIPOFF,
        scoring_policy=policy,
    )
    result = prepared.project(TARGET)
    assert result.projection is not None
    assert any(score == 129.5 for score, _ in result.projection.distribution.weighted_observations)


def test_blocked_internal_only_attempt_ignores_forecasts_and_retains_history_identity() -> None:
    data = replace(history(), opportunities=())
    prepared = batch(data, config=HybridProjectionConfig(use_external=False))
    blocked = prepared.project(TARGET)
    assert blocked == prepared.project(TARGET, forecast=forecast((100, 8, 4, 1, 0.5, 1, 1)))
    changed = batch(
        replace(data, dataset_version="new-evidence"),
        config=HybridProjectionConfig(use_external=False),
    ).project(TARGET)
    assert blocked.failure == changed.failure
    assert blocked.history_fingerprint != changed.history_fingerprint
