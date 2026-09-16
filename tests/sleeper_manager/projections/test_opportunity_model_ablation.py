"""Ablation flags and version fingerprints for the opportunity model."""

from datetime import timedelta

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.opportunity_history import _HistoricalIndex
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from sleeper_manager.projections.opportunity_types import (
    OpportunityModelConfig,
    _cached_opportunity_version,
)
from tests.sleeper_manager.projections.opportunity_model_support import (
    NOW,
    _ablation_fixture,
    row,
)


def test_pace_ablation_neutralizes_only_the_pace_component() -> None:
    dataset, policy = _ablation_fixture()

    full = InterpretableOpportunityModel().project(
        dataset, player_id="player-1", game_id="target", scoring_policy=policy
    )
    no_pace = InterpretableOpportunityModel(OpportunityModelConfig(disable_pace=True)).project(
        dataset, player_id="player-1", game_id="target", scoring_policy=policy
    )

    full_components = {component.code: component for component in full.components}
    no_pace_components = {component.code: component for component in no_pace.components}

    assert full_components["pace"].estimate != 1.0
    assert no_pace_components["pace"].estimate == 1.0
    assert no_pace_components["pace"].adjustment == 0.0
    for code in (
        "opponent_defense",
        "rest",
        "travel",
        "availability",
        "minutes",
        "production_rate",
    ):
        assert full_components[code] == no_pace_components[code]


def test_defense_ablation_neutralizes_only_the_defense_component() -> None:
    dataset, policy = _ablation_fixture()

    full = InterpretableOpportunityModel().project(
        dataset, player_id="player-1", game_id="target", scoring_policy=policy
    )
    no_defense = InterpretableOpportunityModel(
        OpportunityModelConfig(disable_defense=True)
    ).project(dataset, player_id="player-1", game_id="target", scoring_policy=policy)

    full_components = {component.code: component for component in full.components}
    no_defense_components = {component.code: component for component in no_defense.components}

    assert full_components["opponent_defense"].estimate != 1.0
    assert no_defense_components["opponent_defense"].estimate == 1.0
    assert no_defense_components["opponent_defense"].adjustment == 0.0
    for code in ("pace", "rest", "travel", "availability", "minutes", "production_rate"):
        assert full_components[code] == no_defense_components[code]


def test_ablation_semantics_change_model_and_input_version() -> None:
    dataset, policy = _ablation_fixture()

    full = InterpretableOpportunityModel().project(
        dataset, player_id="player-1", game_id="target", scoring_policy=policy
    )
    no_pace = InterpretableOpportunityModel(OpportunityModelConfig(disable_pace=True)).project(
        dataset, player_id="player-1", game_id="target", scoring_policy=policy
    )
    no_defense = InterpretableOpportunityModel(
        OpportunityModelConfig(disable_defense=True)
    ).project(dataset, player_id="player-1", game_id="target", scoring_policy=policy)

    model_versions = {full.model_version, no_pace.model_version, no_defense.model_version}
    input_versions = {full.input_version, no_pace.input_version, no_defense.input_version}
    assert len(model_versions) == 3
    assert len(input_versions) == 3


def test_player_prior_fingerprint_tracks_strict_prefix() -> None:
    """Identify one player's prior prefix without reserializing it per projection."""
    policy = ScoringPolicy(points=1)
    priors = (
        row("g1", NOW - timedelta(days=3), did_play=True, minutes=20, points=10),
        row("g2", NOW - timedelta(days=2), did_play=False, minutes=None, points=0),
        row("g3", NOW - timedelta(days=1), did_play=True, minutes=30, points=30),
    )
    target = row("target", NOW, did_play=False, minutes=None, points=0)
    index = _HistoricalIndex("fixture", policy, 14.0)
    index.resolve_target((*priors, target), "player-1", "target")

    full = index.player_prior_fingerprint("player-1", NOW)
    assert full != "empty"
    assert index.player_prior_fingerprint("player-1", NOW) == full
    assert index.player_prior_fingerprint("player-1", NOW - timedelta(days=10)) == "empty"
    assert index.player_prior_fingerprint("unknown-player", NOW) == "empty"

    short = _HistoricalIndex("fixture", policy, 14.0)
    short_target = row("short-target", NOW, did_play=False, minutes=None, points=0)
    short.resolve_target((priors[0], priors[1], short_target), "player-1", "short-target")
    assert index.player_prior_fingerprint("player-1", priors[2].game_start) == (
        short.player_prior_fingerprint("player-1", NOW)
    )

    changed = _HistoricalIndex("fixture", policy, 14.0)
    changed_prior = row("g1", NOW - timedelta(days=3), did_play=True, minutes=20, points=11)
    changed.resolve_target((changed_prior, priors[1], priors[2], target), "player-1", "target")
    assert changed.player_prior_fingerprint("player-1", NOW) != full


def test_opportunity_input_version_covers_player_prior_fingerprint() -> None:
    """Keep the v2 input identity sensitive to player-prior outcomes."""
    prior = row("prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=20)
    target = row("target", NOW, did_play=False, minutes=None, points=0)
    policy = ScoringPolicy(points=1)

    def version(prior_row: HistoricalFeatureRow) -> str:
        return (
            InterpretableOpportunityModel()
            .project(
                HistoricalFeatureDataset("fixture", "5", NOW, (), (prior_row, target)),
                player_id="player-1",
                game_id="target",
                scoring_policy=policy,
            )
            .input_version
        )

    baseline = version(prior)
    assert baseline.startswith("inputs-v2-")
    changed_points = row("prior", NOW - timedelta(days=1), did_play=True, minutes=30, points=21)
    assert version(changed_points) != baseline
    changed_participation = row(
        "prior", NOW - timedelta(days=1), did_play=False, minutes=None, points=0
    )
    assert version(changed_participation) != baseline


def test_opportunity_config_version_reuses_cached_digest() -> None:
    _cached_opportunity_version.cache_clear()
    config = OpportunityModelConfig()

    assert OpportunityModelConfig().model_version == config.model_version
    assert OpportunityModelConfig(disable_pace=True).model_version != config.model_version
    info = _cached_opportunity_version.cache_info()
    assert (info.hits, info.misses) == (2, 2)
