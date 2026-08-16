from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import replace
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
    OpportunityModelError,
)

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


class _GrowingPrefix(Sequence[HistoricalFeatureRow]):
    """Minimal stand-in for the backtest runner's point-in-time row prefix.

    Wraps a fixed chronological tuple and exposes only the first ``prior_count`` rows
    plus one appended ``target`` row, mirroring ``_PointInTimeRows`` in
    ``backtesting/runner.py`` closely enough to exercise the same duck-typed
    ``prior_count`` contract the opportunity model's history index relies on, without
    depending on that private class directly.
    """

    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self._prior_count = prior_count
        self._target = target

    def __len__(self) -> int:
        return self._prior_count + 1

    @property
    def prior_count(self) -> int:
        return self._prior_count

    def __getitem__(self, index: int) -> HistoricalFeatureRow:  # type: ignore[override]
        normalized = index if index >= 0 else len(self) + index
        if normalized == self._prior_count:
            return self._target
        return self._rows[normalized]


def _sanitize(target: HistoricalFeatureRow) -> HistoricalFeatureRow:
    return replace(
        target,
        target_minutes=None,
        target_started=False,
        target_did_play=False,
        target_box_score=BoxScoreLine(),
        target_line_points=0,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
    )


def _growing_dataset(
    rows: tuple[HistoricalFeatureRow, ...],
    game_starts: tuple[datetime, ...],
    target: HistoricalFeatureRow,
) -> HistoricalFeatureDataset:
    prior_count = bisect_left(game_starts, target.game_start)
    return HistoricalFeatureDataset(
        "fixture", "5", NOW, (), _GrowingPrefix(rows, prior_count, _sanitize(target))
    )


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
        HistoricalFeatureDataset("fixture", "5", NOW, (), (prior, league, target)),
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
        HistoricalFeatureDataset("fixture", "5", NOW, (), (prior, league, target)),
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
            HistoricalFeatureDataset("fixture", "5", NOW, (), (prior, league, target)),
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
                "fixture", "5", NOW, (), (player_prior, *other_rows, target, future)
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


def test_growing_prefix_index_matches_static_reference_scan_for_each_target() -> None:
    rows = (
        row(
            "g1",
            NOW - timedelta(days=4),
            did_play=True,
            minutes=30,
            points=20,
            player_id="player-1",
        ),
        row(
            "g1b",
            NOW - timedelta(days=4),
            did_play=True,
            minutes=25,
            points=12,
            player_id="player-2",
        ),
        row(
            "g2",
            NOW - timedelta(days=3),
            did_play=True,
            minutes=28,
            points=18,
            player_id="player-1",
        ),
        row(
            "g3",
            NOW - timedelta(days=2),
            did_play=False,
            minutes=None,
            points=0,
            player_id="player-1",
        ),
        row(
            "g4",
            NOW - timedelta(days=1),
            did_play=True,
            minutes=32,
            points=22,
            player_id="player-1",
        ),
        row(
            "g4b",
            NOW - timedelta(days=1),
            did_play=True,
            minutes=30,
            points=10,
            player_id="player-2",
        ),
        row("g5", NOW, did_play=True, minutes=34, points=25, player_id="player-1"),
    )
    game_starts = tuple(candidate.game_start for candidate in rows)
    targets = [candidate for candidate in rows if candidate.player_id == "player-1"]
    policy = ScoringPolicy(points=1)
    incremental_model = InterpretableOpportunityModel()

    for target in targets:
        growing_dataset = _growing_dataset(rows, game_starts, target)
        prior_count = bisect_left(game_starts, target.game_start)
        reference_dataset = HistoricalFeatureDataset(
            "fixture", "5", NOW, (), (*rows[:prior_count], _sanitize(target))
        )

        incremental_snapshot = incremental_model.project(
            growing_dataset,
            player_id=target.player_id,
            game_id=target.game_id,
            scoring_policy=policy,
        )
        reference_snapshot = InterpretableOpportunityModel().project(
            reference_dataset,
            player_id=target.player_id,
            game_id=target.game_id,
            scoring_policy=policy,
        )

        assert incremental_snapshot.components == reference_snapshot.components
        assert incremental_snapshot.input_version == reference_snapshot.input_version
        assert (
            incremental_snapshot.distribution.weighted_observations
            == reference_snapshot.distribution.weighted_observations
        )


def test_same_tipoff_rows_cannot_observe_each_others_realized_outcome() -> None:
    tipoff = NOW - timedelta(hours=1)
    prior = row("prior", tipoff - timedelta(days=1), did_play=True, minutes=30, points=20)
    target = row("target", tipoff, did_play=False, minutes=None, points=0)

    def project(concurrent: HistoricalFeatureRow) -> tuple[float, str]:
        dataset = HistoricalFeatureDataset("fixture", "5", NOW, (), (prior, concurrent, target))
        snapshot = InterpretableOpportunityModel().project(
            dataset,
            player_id="player-1",
            game_id="target",
            scoring_policy=ScoringPolicy(points=1),
        )
        return snapshot.distribution.expected_value, snapshot.input_version

    low_concurrent = row(
        "concurrent-low", tipoff, did_play=True, minutes=30, points=5, player_id="player-2"
    )
    high_concurrent = row(
        "concurrent-high", tipoff, did_play=True, minutes=30, points=90, player_id="player-2"
    )

    baseline_value, baseline_version = project(low_concurrent)
    changed_value, changed_version = project(high_concurrent)

    assert changed_value == pytest.approx(baseline_value)
    assert changed_version == baseline_version


def test_incompatible_dataset_version_discards_the_stale_index() -> None:
    model = InterpretableOpportunityModel()
    policy = ScoringPolicy(points=1)

    prior_v1 = row("prior-v1", NOW - timedelta(days=1), did_play=True, minutes=30, points=100)
    target_v1 = row("target-v1", NOW, did_play=False, minutes=None, points=0)
    dataset_v1 = HistoricalFeatureDataset("dataset-v1", "5", NOW, (), (prior_v1, target_v1))
    model.project(dataset_v1, player_id="player-1", game_id="target-v1", scoring_policy=policy)
    index_after_v1 = model._index

    later = NOW + timedelta(days=10)
    prior_v2 = row("prior-v2", later - timedelta(days=1), did_play=True, minutes=30, points=5)
    target_v2 = row("target-v2", later, did_play=False, minutes=None, points=0)
    dataset_v2 = HistoricalFeatureDataset("dataset-v2", "5", later, (), (prior_v2, target_v2))
    snapshot_v2 = model.project(
        dataset_v2, player_id="player-1", game_id="target-v2", scoring_policy=policy
    )
    index_after_v2 = model._index

    reference_v2 = InterpretableOpportunityModel().project(
        dataset_v2, player_id="player-1", game_id="target-v2", scoring_policy=policy
    )

    assert index_after_v2 is not index_after_v1
    assert snapshot_v2.input_version == reference_v2.input_version
    assert snapshot_v2.distribution.expected_value == pytest.approx(
        reference_v2.distribution.expected_value
    )

    component = next(
        component for component in snapshot_v2.components if component.code == "production_rate"
    )
    reference_component = next(
        component for component in reference_v2.components if component.code == "production_rate"
    )
    assert component.estimate == pytest.approx(reference_component.estimate)


def test_incompatible_scoring_policy_version_discards_the_stale_index() -> None:
    model = InterpretableOpportunityModel()
    prior = row("prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=20)
    target = row("target", NOW, did_play=False, minutes=None, points=0)
    dataset = HistoricalFeatureDataset("fixture", "5", NOW, (), (prior, target))

    model.project(
        dataset, player_id="player-1", game_id="target", scoring_policy=ScoringPolicy(points=1)
    )
    index_after_first = model._index
    model.project(
        dataset, player_id="player-1", game_id="target", scoring_policy=ScoringPolicy(points=2)
    )
    index_after_second = model._index

    assert index_after_second is not index_after_first


def test_chronological_regression_in_growing_prefix_raises_explicitly() -> None:
    rows = (
        row("g1", NOW - timedelta(days=3), did_play=True, minutes=30, points=20),
        row("g2", NOW - timedelta(days=2), did_play=True, minutes=28, points=18),
        row("g3", NOW - timedelta(days=1), did_play=True, minutes=32, points=22),
        row("g4", NOW, did_play=True, minutes=34, points=25),
    )
    game_starts = tuple(candidate.game_start for candidate in rows)
    model = InterpretableOpportunityModel()
    policy = ScoringPolicy(points=1)

    model.project(
        _growing_dataset(rows, game_starts, rows[3]),
        player_id="player-1",
        game_id="g4",
        scoring_policy=policy,
    )

    with pytest.raises(OpportunityModelError):
        model.project(
            _growing_dataset(rows, game_starts, rows[1]),
            player_id="player-1",
            game_id="g2",
            scoring_policy=policy,
        )
