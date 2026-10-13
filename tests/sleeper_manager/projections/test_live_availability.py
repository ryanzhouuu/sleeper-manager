"""Generic injury reports cannot become confirmed absences or silently clear uncertainty."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, PlayerAvailability, SourceMetadata
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_availability import constrain_live_availability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from tests.sleeper_manager.workflows.test_daily_plan import GAME_START, NOW, _profile, _snapshot

TARGET = LiveProjectionTarget("p1", "g1", GAME_START, "401")


def constrain(*, report=None, generic=None, gap=None):  # type: ignore[no-untyped-def]
    return constrain_live_availability(
        _snapshot("p1", "g1", expected=20),
        TARGET,
        report,
        cutoff=NOW,
        scoring_policy=_profile().scoring,
        max_age=timedelta(minutes=15),
        generic_report=generic,
        availability_gap=gap,
    )


def test_generic_out_is_a_warning_and_does_not_zero_game_production() -> None:
    generic = PlayerAvailability(
        "401", AvailabilityStatus.OUT, None, SourceMetadata("espn", "401", NOW)
    )
    result = constrain(generic=generic)
    assert result.distribution.expected_value == 20
    assert result.reasons[-1].code == "availability_uncertainty"
    assert "unverified" in result.reasons[-1].message


@pytest.mark.parametrize("kind", ["stale", "future", "wrong_player"])
def test_inapplicable_generic_evidence_does_not_change_projection(kind: str) -> None:
    source = SourceMetadata(
        "espn",
        "401",
        NOW - timedelta(minutes=16) if kind == "stale" else NOW,
        NOW + timedelta(seconds=1) if kind == "future" else None,
    )
    generic = PlayerAvailability(
        "other" if kind == "wrong_player" else "401", AvailabilityStatus.OUT, None, source
    )
    assert constrain(generic=generic) == _snapshot("p1", "g1", expected=20)


def test_questionable_revision_changes_input_identity_without_fabricating_a_percentage() -> None:
    report = GameAvailability(
        "g1",
        NOW,
        AvailabilityStatus.QUESTIONABLE,
        AvailabilityObservation.REPORTED,
        "official:first",
    )
    first = constrain(report=report)
    second = constrain(report=replace(report, source_version="official:second"))
    assert first.distribution == second.distribution
    assert first.input_version != second.input_version
    assert first.reasons[-1].message == second.reasons[-1].message


def test_missing_official_source_retains_a_visible_data_gap() -> None:
    result = constrain(gap="official_availability_failed:RuntimeError")
    assert result.distribution.expected_value == 20
    assert result.reasons[-1].code == "availability_data_gap"
