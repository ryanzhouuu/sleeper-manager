from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import ProjectionFallback
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
    PaceStatsFallback,
)
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def row(
    game_id: str,
    game_start: datetime,
    *,
    did_play: bool,
    minutes: float | None,
    points: int,
    pace_factor: float | None = None,
) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="fixture",
        available_as_of=game_start - timedelta(minutes=30),
        player_id="player-1",
        sleeper_id="sleeper-1",
        game_id=game_id,
        game_start=game_start,
        team_id="team-1",
        opponent_team_id="team-2",
        opponent_abbreviation="t2",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.AVAILABLE,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=minutes,
        target_started=did_play,
        target_did_play=did_play,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(),
        opponent_defensive_rating=100,
        opponent_sample_size=5,
        own_team_pace=100,
        own_team_pace_fallback=PaceStatsFallback.OBSERVED,
        expected_matchup_pace=100,
        baseline_exposure_pace=100,
        pace_factor=pace_factor,
    )


def test_projection_exposes_components_and_matches_full_mixture_decomposition() -> None:
    prior_a = row("game-1", NOW - timedelta(days=3), did_play=True, minutes=30, points=20)
    prior_b = row("game-2", NOW - timedelta(days=2), did_play=True, minutes=20, points=10)
    target = row("target", NOW, did_play=False, minutes=None, points=0, pace_factor=1.04)
    dataset = HistoricalFeatureDataset("fixture", "3", NOW, (), (prior_a, prior_b, target))

    snapshot = InterpretableOpportunityModel().project(
        dataset,
        player_id="player-1",
        game_id="target",
        scoring_policy=ScoringPolicy(points=1),
    )

    components = {component.code: component for component in snapshot.components}
    environment = (
        components["pace"].estimate
        * components["opponent_defense"].estimate
        * components["rest"].estimate
        * components["travel"].estimate
    )
    expected = components["availability"].estimate * components["minutes"].estimate
    expected *= components["production_rate"].estimate * environment
    assert snapshot.distribution.expected_value == pytest.approx(expected, abs=1e-5)
    assert components["availability"].estimate < 1
    assert sum(
        weight for _, weight in snapshot.distribution.weighted_observations
    ) == pytest.approx(1)
    assert snapshot.distribution.weighted_observations[0][0] == 0


def test_missing_history_uses_explicit_fallback_and_zero_outcome() -> None:
    target = row("target", NOW, did_play=False, minutes=None, points=0)
    dataset = HistoricalFeatureDataset("fixture", "3", NOW, (), (target,))

    snapshot = InterpretableOpportunityModel().project(
        dataset,
        player_id="player-1",
        game_id="target",
        scoring_policy=ScoringPolicy(points=1),
    )

    assert snapshot.distribution.expected_value == 0
    assert snapshot.distribution.weighted_observations == ((0.0, 1.0),)
    assert snapshot.components[1].fallback is ProjectionFallback.LEAGUE_AVERAGE
