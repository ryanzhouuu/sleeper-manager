"""Regression checks for evidence that normalized defaults cannot establish."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    ParticipationOpportunity,
)

AT = datetime(2026, 1, 1, tzinfo=UTC)


def outcome() -> JointGameObservation:
    return JointGameObservation(
        "p",
        "g",
        AT,
        AT + timedelta(hours=3),
        True,
        30,
        BoxScoreLine(points=20),
        ("points",),
        "source-v1",
    )


def test_config_identity_binds_every_default() -> None:
    config = HybridProjectionConfig()
    assert config.model_version == HybridProjectionConfig().model_version
    for name, value in (
        ("use_external", False),
        ("donor_count", 10),
        ("forecast_max_age_hours", 24.0),
    ):
        assert replace(config, **{name: value}).model_version != config.model_version


@pytest.mark.parametrize(
    "changes",
    [
        {"donor_count": True},
        {"minimum_donors": 21},
        {"fit_iterations": 1.5},
        {"recency_half_life_days": float("nan")},
        {"maximum_game_weight": 2},
        {"profile_scale_floors": (1.0,)},
    ],
)
def test_config_rejects_invalid_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(HybridProjectionError):
        HybridProjectionConfig(**changes)  # type: ignore[arg-type]


def test_field_coverage_is_explicit_even_for_zero_values() -> None:
    row = outcome()
    assert "rebounds" not in row.verified_fields
    assert row.line.rebounds == 0
    with pytest.raises(HybridProjectionError, match="unknown_outcome_field"):
        replace(row, verified_fields=("invented",))


def test_invalid_timestamps_and_box_scores_are_rejected() -> None:
    with pytest.raises(HybridProjectionError, match="naive_timestamp"):
        replace(outcome(), finalized_at=AT.replace(tzinfo=None))
    with pytest.raises(HybridProjectionError, match="incoherent_box_score"):
        replace(
            outcome(),
            line=BoxScoreLine(points=2, three_pointers_made=1),
            verified_fields=("points", "three_pointers_made"),
        )
    with pytest.raises(HybridProjectionError, match="invalid_box_score"):
        replace(outcome(), line=BoxScoreLine(points=-1))


def test_duplicates_and_contradictory_census_are_rejected() -> None:
    row = outcome()
    with pytest.raises(HybridProjectionError, match="duplicate_player_game"):
        HybridHistory("v1", (row, row), ())
    opportunity = ParticipationOpportunity("p", "g", AT, row.finalized_at, False, "census-v1")
    with pytest.raises(HybridProjectionError, match="contradictory_participation"):
        HybridHistory("v1", (row,), (opportunity,))
