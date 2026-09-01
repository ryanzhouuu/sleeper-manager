"""Regression coverage for incremental direct-baseline backtest history."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting import BacktestConfig, BacktestModel, run_backtest
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineError,
)

BASE = datetime(2025, 1, 1, 18, tzinfo=UTC)
POLICY = ScoringPolicy(points=1)


def row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
    *,
    finalized_at: datetime | None = None,
    source_hash: str = "source-v1",
) -> HistoricalFeatureRow:
    """Build one finalized historical feature row with deterministic provenance."""
    source = SourceMetadata(
        "fixture",
        game_id,
        start + timedelta(hours=3),
        content_hash=source_hash,
    )
    return HistoricalFeatureRow(
        dataset_version="incremental-fixture",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=player_id,
        game_id=game_id,
        game_start=start,
        outcome_finalized_at=finalized_at or start + timedelta(hours=2),
        team_id="CHI",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
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
        target_minutes=30,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(source,),
    )


def feature_dataset(
    rows: Sequence[HistoricalFeatureRow],
    *,
    version: str = "incremental-fixture",
) -> HistoricalFeatureDataset:
    """Wrap fixture rows in the immutable historical dataset contract."""
    return HistoricalFeatureDataset(version, "5", BASE, (), rows)


def sanitized(target: HistoricalFeatureRow) -> HistoricalFeatureRow:
    """Remove realized target fields exactly as the backtest runner does."""
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


class GrowingRows(Sequence[HistoricalFeatureRow]):
    """Expose a chronological prior prefix followed by one sanitized target."""

    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self.prior_count = prior_count
        self._target = sanitized(target)

    def __len__(self) -> int:
        """Include the uncommitted target after the legitimate prior prefix."""
        return self.prior_count + 1

    def __getitem__(self, index: int) -> HistoricalFeatureRow:
        """Return a prior row or the appended target without copying the prefix."""
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        if normalized == self.prior_count:
            return self._target
        return self._rows[normalized]


def growing_dataset(
    rows: tuple[HistoricalFeatureRow, ...],
    target: HistoricalFeatureRow,
    *,
    version: str = "incremental-fixture",
) -> HistoricalFeatureDataset:
    """Build the backtest runner's growing point-in-time dataset shape."""
    starts = tuple(candidate.game_start for candidate in rows)
    prior_count = bisect_left(starts, target.game_start)
    return feature_dataset(GrowingRows(rows, prior_count, target), version=version)


def explicit_snapshot(
    rows: tuple[HistoricalFeatureRow, ...],
    target: HistoricalFeatureRow,
    *,
    scoring_policy: ScoringPolicy = POLICY,
) -> ProjectionSnapshot:
    """Project one target through the production-neutral explicit-history path."""
    history = tuple(
        DirectBaselineObservation.from_historical_row(candidate)
        for candidate in rows
        if candidate.game_start < target.game_start
    )
    request = PregameProjectionRequest(
        dataset_version="incremental-fixture",
        feature_schema_version="5",
        player_id=target.player_id,
        game_id=target.game_id,
        game_start=target.game_start,
        available_as_of=target.available_as_of,
        history=history,
        history_player_id=target.player_id,
    )
    return DirectFantasyPointBaseline().project_pregame(
        request,
        scoring_policy=scoring_policy,
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
    assert all(snapshot.input_version.startswith("projection-input-v4-") for snapshot in actual)


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


def test_input_v4_tracks_prior_provenance_target_metadata_and_scoring() -> None:
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
    assert baseline.startswith("projection-input-v4-")
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


class ExplicitHistoryProjector:
    """Test adapter that reconstructs compact history for every backtest target."""

    def __init__(self) -> None:
        self.baseline = DirectFantasyPointBaseline()

    @property
    def model_version(self) -> str:
        """Match the wrapped-name behavior used by calibrated direct projections."""
        return "DirectFantasyPointBaseline"

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        """Delegate through an explicit request instead of the incremental row contract."""
        target = dataset.rows[-1]
        history = tuple(
            DirectBaselineObservation.from_historical_row(candidate)
            for candidate in dataset.rows
            if candidate.game_start < target.game_start
        )
        request = PregameProjectionRequest(
            dataset_version=dataset.dataset_version,
            feature_schema_version=dataset.feature_schema_version,
            player_id=player_id,
            game_id=game_id,
            game_start=target.game_start,
            available_as_of=target.available_as_of,
            history=history,
            history_player_id=player_id,
            source_versions=dataset.source_versions,
        )
        return self.baseline.project_pregame(
            request,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )


def test_backtest_reports_match_explicit_reference_for_raw_and_calibrated_direct_models() -> None:
    """Preserve observations, skips, cohorts, metrics, and comparisons end to end."""
    rows = tuple(
        row(f"{player}-g{day}", player, BASE + timedelta(days=day), 10 + day * 2 + offset)
        for day in range(5)
        for player, offset in (("p1", 0), ("p2", 5))
    )
    dataset = feature_dataset(rows)
    config = BacktestConfig(min_prior_games=2)
    incremental = run_backtest(
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
    explicit = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", ExplicitHistoryProjector()),
            BacktestModel(
                "calibrated",
                CalibratedProjectionModel(
                    ExplicitHistoryProjector(), min_samples=1, refresh_interval=1
                ),
            ),
        ),
        config=config,
    )

    assert incremental.dataset_version == explicit.dataset_version
    assert incremental.scoring_policy_version == explicit.scoring_policy_version
    assert incremental.config_version == explicit.config_version
    assert incremental.target_count == explicit.target_count
    assert incremental.target_skips == explicit.target_skips
    assert incremental.reference_model == explicit.reference_model
    assert incremental.comparisons == explicit.comparisons
    for incremental_result, explicit_result in zip(
        incremental.model_results,
        explicit.model_results,
        strict=True,
    ):
        assert incremental_result.model.name == explicit_result.model.name
        assert incremental_result.observations == explicit_result.observations
        assert incremental_result.skips == explicit_result.skips
        assert incremental_result.metrics == explicit_result.metrics
        assert incremental_result.cohort_diagnostics == explicit_result.cohort_diagnostics
