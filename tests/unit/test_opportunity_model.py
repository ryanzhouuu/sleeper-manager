from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import ProjectionFallback
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
    OpponentStatsFallback,
    PaceStatsFallback,
)
from sleeper_manager.projections.opportunity_model import (
    EnvironmentModel,
    InterpretableOpportunityModel,
)

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def row(
    game_id: str,
    game_start: datetime,
    *,
    did_play: bool,
    minutes: float | None,
    points: int,
    pace_factor: float | None = None,
    player_id: str = "player-1",
    opponent_defensive_rating: float | None = 100,
    league_defensive_rating: float | None = None,
    opponent_stats_fallback: OpponentStatsFallback = OpponentStatsFallback.OBSERVED,
) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="fixture",
        available_as_of=game_start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=player_id,
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
        opponent_defensive_rating=opponent_defensive_rating,
        league_defensive_rating=league_defensive_rating,
        opponent_sample_size=5,
        opponent_stats_fallback=opponent_stats_fallback,
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


def test_sparse_high_rate_player_shrinks_toward_lower_independent_league_rate() -> None:
    prior = row("player-prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=30)
    league = row(
        "league-prior",
        NOW - timedelta(days=1),
        did_play=True,
        minutes=30,
        points=15,
        player_id="player-2",
    )
    target = row("target", NOW, did_play=False, minutes=None, points=0)

    snapshot = InterpretableOpportunityModel().project(
        HistoricalFeatureDataset("fixture", "4", NOW, (), (prior, league, target)),
        player_id="player-1",
        game_id="target",
        scoring_policy=ScoringPolicy(points=1),
    )

    component = next(
        component for component in snapshot.components if component.code == "production_rate"
    )
    assert component.baseline == pytest.approx(1.0)
    shrinkage = component.effective_sample / (component.effective_sample + 120)
    assert component.estimate == pytest.approx(shrinkage + 0.5 * (1 - shrinkage))
    assert 0.5 < component.estimate < component.baseline
    assert component.adjustment == pytest.approx(component.estimate - component.baseline)
    assert component.fallback is ProjectionFallback.SHRUNK
    assert "independent league" in component.message


def test_sparse_low_rate_player_shrinks_toward_higher_independent_league_rate() -> None:
    prior = row("player-prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=15)
    league = row(
        "league-prior",
        NOW - timedelta(days=1),
        did_play=True,
        minutes=30,
        points=30,
        player_id="player-2",
    )
    target = row("target", NOW, did_play=False, minutes=None, points=0)

    snapshot = InterpretableOpportunityModel().project(
        HistoricalFeatureDataset("fixture", "4", NOW, (), (prior, league, target)),
        player_id="player-1",
        game_id="target",
        scoring_policy=ScoringPolicy(points=1),
    )

    component = next(
        component for component in snapshot.components if component.code == "production_rate"
    )
    assert component.baseline == pytest.approx(0.5)
    shrinkage = component.effective_sample / (component.effective_sample + 120)
    assert component.estimate == pytest.approx(0.5 * shrinkage + 1.0 * (1 - shrinkage))
    assert component.estimate > component.baseline


def test_more_player_minutes_reduce_production_shrinkage() -> None:
    league = row(
        "league-prior",
        NOW - timedelta(days=1),
        did_play=True,
        minutes=30,
        points=15,
        player_id="player-2",
    )
    target = row("target", NOW, did_play=False, minutes=None, points=0)

    def estimate(minutes: float, game_id: str) -> float:
        prior = row(
            game_id,
            NOW - timedelta(days=2),
            did_play=True,
            minutes=minutes,
            points=int(minutes),
        )
        snapshot = InterpretableOpportunityModel().project(
            HistoricalFeatureDataset("fixture", "4", NOW, (), (prior, league, target)),
            player_id="player-1",
            game_id="target",
            scoring_policy=ScoringPolicy(points=1),
        )
        return next(
            component.estimate
            for component in snapshot.components
            if component.code == "production_rate"
        )

    low_minutes = estimate(20, "low-minutes")
    high_minutes = estimate(120, "high-minutes")
    assert abs(high_minutes - 1.0) < abs(low_minutes - 1.0)


def test_prior_league_outcome_changes_projection_and_input_version_but_future_does_not() -> None:
    player_prior = row(
        "player-prior", NOW - timedelta(days=2), did_play=True, minutes=30, points=20
    )
    other_prior = row(
        "other-prior",
        NOW - timedelta(days=2),
        did_play=True,
        minutes=30,
        points=10,
        player_id="player-2",
    )
    target = row("target", NOW, did_play=False, minutes=None, points=0)

    def project(other_rows: tuple[HistoricalFeatureRow, ...], *, future_points: int = 100):
        future = row(
            "other-future",
            NOW + timedelta(days=1),
            did_play=True,
            minutes=30,
            points=future_points,
            player_id="player-2",
        )
        return InterpretableOpportunityModel().project(
            HistoricalFeatureDataset(
                "fixture", "4", NOW, (), (player_prior, *other_rows, target, future)
            ),
            player_id="player-1",
            game_id="target",
            scoring_policy=ScoringPolicy(points=1),
        )

    baseline = project((other_prior,))
    changed_prior = project(
        (
            row(
                "other-prior",
                NOW - timedelta(days=2),
                did_play=True,
                minutes=30,
                points=30,
                player_id="player-2",
            ),
        )
    )
    changed_future = project((other_prior,), future_points=5)

    baseline_component = next(
        component for component in baseline.components if component.code == "production_rate"
    )
    changed_component = next(
        component for component in changed_prior.components if component.code == "production_rate"
    )
    assert changed_component.estimate != baseline_component.estimate
    assert changed_prior.input_version != baseline.input_version
    assert changed_future.input_version == baseline.input_version


def test_no_league_prior_keeps_player_rate_without_claiming_shrinkage() -> None:
    prior = row("player-prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=30)
    target = row("target", NOW, did_play=False, minutes=None, points=0)
    model = InterpretableOpportunityModel()

    estimate = model.production_rate.estimate(
        target,
        (prior,),
        ScoringPolicy(points=1),
        league_prior_rows=(),
    )

    assert estimate.value == pytest.approx(1.0)
    assert estimate.fallback is ProjectionFallback.MISSING
    assert "without shrinkage" in estimate.message


def test_opponent_defense_is_neutral_at_league_baseline_and_has_correct_direction() -> None:
    model = EnvironmentModel(InterpretableOpportunityModel().config)

    def defense(opponent_rating: float | None, league_rating: float | None):
        target = row(
            "target",
            NOW,
            did_play=False,
            minutes=None,
            points=0,
            opponent_defensive_rating=opponent_rating,
            league_defensive_rating=league_rating,
        )
        return model.estimate(target)[1]

    neutral = defense(100, 100)
    worse = defense(110, 100)
    better = defense(90, 100)

    assert neutral.value == pytest.approx(1.0)
    assert neutral.fallback is ProjectionFallback.OBSERVED
    assert "100.00" in neutral.message
    assert worse.value > 1.0
    assert better.value < 1.0


def test_opponent_defense_factor_is_clipped_and_missing_inputs_are_neutral() -> None:
    model = EnvironmentModel(InterpretableOpportunityModel().config)

    def defense(opponent_rating: float | None, league_rating: float | None):
        target = row(
            "target",
            NOW,
            did_play=False,
            minutes=None,
            points=0,
            opponent_defensive_rating=opponent_rating,
            league_defensive_rating=league_rating,
        )
        return model.estimate(target)[1]

    assert defense(200, 100).value == pytest.approx(1.1)
    assert defense(1, 100).value == pytest.approx(0.9)
    for opponent_rating, league_rating in ((None, 100), (100, None), (100, 0)):
        estimate = defense(opponent_rating, league_rating)
        assert estimate.value == pytest.approx(1.0)
        assert estimate.fallback is ProjectionFallback.MISSING
        assert "neutral" in estimate.message


def test_opponent_defense_preserves_historical_fallback_metadata() -> None:
    target = row(
        "target",
        NOW,
        did_play=False,
        minutes=None,
        points=0,
        opponent_defensive_rating=110,
        league_defensive_rating=100,
        opponent_stats_fallback=OpponentStatsFallback.SHRUNK,
    )

    estimate = EnvironmentModel(InterpretableOpportunityModel().config).estimate(target)[1]

    assert estimate.value == pytest.approx(1.1)
    assert estimate.fallback is ProjectionFallback.SHRUNK
    assert "prior league baseline" in estimate.message
