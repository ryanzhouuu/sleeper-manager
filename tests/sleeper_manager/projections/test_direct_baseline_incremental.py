"""Incremental history index, fingerprints, and same-tipoff exclusion."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.backtesting import BacktestConfig, BacktestModel, run_backtest
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    DatasetSourceVersion,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineError,
)
from tests.sleeper_manager.projections.direct_baseline_incremental_support import (
    BASE,
    POLICY,
    explicit_snapshot,
    feature_dataset,
    growing_dataset,
    row,
)


def test_incremental_path_matches_explicit_history_and_compacts_each_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve exact projections while converting only newly revealed rows."""
    rows = (
        row("p1-g1", "p1", BASE, 10),
        row("p2-g1", "p2", BASE, 20),
        row("p1-g2", "p1", BASE + timedelta(days=1), 15),
        row("p2-g2", "p2", BASE + timedelta(days=1), 25),
        row("p1-g3", "p1", BASE + timedelta(days=2), 30),
    )
    targets = (rows[2], rows[4])
    expected = tuple(explicit_snapshot(rows, target) for target in targets)
    original = DirectBaselineObservation.from_historical_row.__func__
    conversions = 0

    def counting_conversion(
        cls: type[DirectBaselineObservation], candidate: HistoricalFeatureRow
    ) -> DirectBaselineObservation:
        """Count feature-row compaction while delegating to the production constructor."""
        nonlocal conversions
        conversions += 1
        return original(cls, candidate)

    monkeypatch.setattr(
        DirectBaselineObservation,
        "from_historical_row",
        classmethod(counting_conversion),
    )
    model = DirectFantasyPointBaseline()
    actual = tuple(
        model.project(
            growing_dataset(rows, target),
            player_id=target.player_id,
            game_id=target.game_id,
            scoring_policy=POLICY,
        )
        for target in targets
    )

    assert actual == expected
    assert conversions == 4
    assert all(snapshot.input_version.startswith("projection-input-v5-") for snapshot in actual)


def test_incremental_path_projects_when_prior_outcome_finalization_is_missing() -> None:
    """Treat cached historical rows without finalization timestamps as tipoff-eligible."""
    prior_one = row("prior-1", "p1", BASE, 10, omit_finalization=True)
    prior_two = row("prior-2", "p1", BASE + timedelta(days=1), 20, omit_finalization=True)
    target = row("target", "p1", BASE + timedelta(days=2), 0)
    rows = (prior_one, prior_two, target)
    reference_rows = (
        row("prior-1", "p1", BASE, 10),
        row("prior-2", "p1", BASE + timedelta(days=1), 20),
        target,
    )

    incremental = DirectFantasyPointBaseline().project(
        growing_dataset(rows, target),
        player_id="p1",
        game_id="target",
        scoring_policy=POLICY,
    )
    reference = DirectFantasyPointBaseline().project(
        growing_dataset(reference_rows, target),
        player_id="p1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert incremental.distribution == reference.distribution
    assert incremental.input_version.startswith("projection-input-v5-")
    assert incremental.input_version != reference.input_version


def test_backtest_covers_direct_models_when_prior_outcome_finalization_is_missing() -> None:
    """Smoke coverage for the cached-dataset regression that skipped every target."""
    rows = tuple(
        row(
            f"p1-g{day}",
            "p1",
            BASE + timedelta(days=day),
            10 + day * 2,
            omit_finalization=True,
        )
        for day in range(3)
    )
    dataset = feature_dataset(rows)
    config = BacktestConfig(min_prior_games=1)
    result = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", DirectFantasyPointBaseline()),
            BacktestModel(
                "calibrated",
                CalibratedProjectionModel(
                    DirectFantasyPointBaseline(), min_samples=1, refresh_interval=1
                ),
            ),
        ),
        config=config,
    )

    for model_result in result.model_results:
        assert model_result.metrics.sample_count == model_result.metrics.target_count
        assert model_result.metrics.sample_count > 0
        assert model_result.metrics.coverage == 1.0


def test_incremental_fingerprint_includes_missing_finalization_history() -> None:
    """Hash prior rows without outcome_finalized_at instead of treating history as empty."""
    prior = row("prior", "p1", BASE, 10, omit_finalization=True)
    target = row("target", "p1", BASE + timedelta(days=1), 0)
    rows = (prior, target)

    snapshot = DirectFantasyPointBaseline().project(
        growing_dataset(rows, target),
        player_id="p1",
        game_id="target",
        scoring_policy=POLICY,
    )

    with pytest.raises(ProjectionBaselineError, match="No prior same-season"):
        DirectFantasyPointBaseline().project(
            growing_dataset((target,), target),
            player_id="p1",
            game_id="target",
            scoring_policy=POLICY,
        )

    assert snapshot.input_version.startswith("projection-input-v5-")

    changed_prior = replace(prior, target_line_points=11, target_box_score=BoxScoreLine(points=11))
    changed = DirectFantasyPointBaseline().project(
        growing_dataset((changed_prior, target), target),
        player_id="p1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert changed.input_version != snapshot.input_version


def test_incremental_path_excludes_same_tipoff_and_unfinalized_prior_outcomes() -> None:
    """Keep concurrent and not-yet-finalized results outside the prepared history."""
    target_start = BASE + timedelta(days=2)
    prior = row("prior", "p1", BASE, 10)
    overlapping = row(
        "overlapping",
        "p2",
        target_start - timedelta(hours=1),
        100,
        finalized_at=target_start + timedelta(hours=1),
    )
    same_tipoff = row("same", "p2", target_start, 100)
    target = row("target", "p1", target_start, 0)
    rows = (prior, overlapping, same_tipoff, target)

    incremental = DirectFantasyPointBaseline().project(
        growing_dataset(rows, target),
        player_id="p1",
        game_id="target",
        scoring_policy=POLICY,
    )
    reference = explicit_snapshot((prior, target), target)

    assert incremental == reference


def test_incremental_path_rejects_regression_and_rebuilds_replacement_sequence() -> None:
    """Fail backward movement but safely rebuild unrelated same-version histories."""
    original = (
        row("a1", "p1", BASE, 10),
        row("a2", "p1", BASE + timedelta(days=1), 20),
        row("a3", "p1", BASE + timedelta(days=2), 30),
    )
    model = DirectFantasyPointBaseline()
    model.project(
        growing_dataset(original, original[2]),
        player_id="p1",
        game_id="a3",
        scoring_policy=POLICY,
    )
    with pytest.raises(ProjectionBaselineError, match="regression"):
        model.project(
            growing_dataset(original, original[1]),
            player_id="p1",
            game_id="a2",
            scoring_policy=POLICY,
        )

    replacement = (
        row("b1", "p1", BASE + timedelta(days=10), 5),
        row("b2", "p1", BASE + timedelta(days=11), 7),
        row("b3", "p1", BASE + timedelta(days=12), 9),
    )
    rebuilt = model.project(
        growing_dataset(replacement, replacement[2]),
        player_id="p1",
        game_id="b3",
        scoring_policy=POLICY,
    )
    fresh = DirectFantasyPointBaseline().project(
        growing_dataset(replacement, replacement[2]),
        player_id="p1",
        game_id="b3",
        scoring_policy=POLICY,
    )

    assert rebuilt == fresh


def test_incremental_path_rejects_duplicate_prior_player_games() -> None:
    """Fail explicitly before duplicate history can contaminate model aggregates."""
    prior = row("duplicate", "p1", BASE, 10)
    duplicate = replace(prior, game_start=BASE + timedelta(hours=1))
    target = row("target", "p1", BASE + timedelta(days=1), 20)

    with pytest.raises(ProjectionBaselineError, match="Duplicate direct history"):
        DirectFantasyPointBaseline().project(
            growing_dataset((prior, duplicate, target), target),
            player_id="p1",
            game_id="target",
            scoring_policy=POLICY,
        )


def test_input_v5_tracks_prior_provenance_target_metadata_and_scoring() -> None:
    """Fingerprint every modeled input while continuing to ignore realized target output."""
    prior = DirectBaselineObservation.from_historical_row(row("prior", "p1", BASE, 10))
    target_start = BASE + timedelta(days=1)
    request = PregameProjectionRequest(
        dataset_version="incremental-fixture",
        feature_schema_version="5",
        player_id="p1",
        game_id="target",
        game_start=target_start,
        available_as_of=target_start - timedelta(minutes=30),
        history=(prior,),
    )

    def version(
        candidate: PregameProjectionRequest,
        scoring_policy: ScoringPolicy = POLICY,
    ) -> str:
        """Return one direct input identity for concise mutation assertions."""
        return (
            DirectFantasyPointBaseline()
            .project_pregame(
                candidate,
                scoring_policy=scoring_policy,
            )
            .input_version
        )

    baseline = version(request)
    assert baseline.startswith("projection-input-v5-")
    changed_score = replace(prior, box_score=BoxScoreLine(points=11))
    assert version(replace(request, history=(changed_score,))) != baseline
    assert (
        version(
            replace(
                request,
                history=(
                    replace(
                        prior,
                        outcome_finalized_at=prior.outcome_finalized_at + timedelta(minutes=1),
                    ),
                ),
            )
        )
        != baseline
    )
    changed_source = replace(prior, source_version="source-v2")
    assert version(replace(request, history=(changed_source,))) != baseline
    changed_cutoff = replace(
        request,
        available_as_of=request.available_as_of - timedelta(minutes=1),
    )
    assert version(changed_cutoff) != baseline
    assert version(replace(request, game_id="other-target")) != baseline
    assert version(request, ScoringPolicy(points=2)) != baseline
    changed_sources = replace(
        request,
        source_versions=(DatasetSourceVersion("fixture", "v2", ("game-1",)),),
    )
    assert version(changed_sources) != baseline
    changed_source_ids = replace(
        request,
        source_versions=(DatasetSourceVersion("fixture", "v2", ("game-1", "game-2")),),
    )
    assert version(changed_source_ids) != version(changed_sources)
