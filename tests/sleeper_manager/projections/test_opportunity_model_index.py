"""Growing-prefix index and cache invalidation for the opportunity model."""

from bisect import bisect_left
from datetime import timedelta

import pytest

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from sleeper_manager.projections.opportunity_types import (
    OpportunityModelError,
)
from tests.sleeper_manager.projections.opportunity_model_support import (
    NOW,
    _growing_dataset,
    _sanitize,
    row,
)


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
