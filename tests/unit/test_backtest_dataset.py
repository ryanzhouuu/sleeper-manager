from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestModel,
    ChronologicalFold,
    run_backtest,
    run_validation_folds,
)
from sleeper_manager.backtesting.backtest_dataset import (
    PreparedBacktestDataset,
    prepare_backtest_dataset,
)
from sleeper_manager.backtesting.models import BacktestError
from sleeper_manager.backtesting.progress import (
    ProgressEvent,
    ProgressMode,
    ProgressReporter,
    ProgressStage,
    ProgressState,
)
from sleeper_manager.backtesting.validation import folds as folds_module
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 9, 1, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


def row(game_id: str, player_id: str, start: datetime) -> HistoricalFeatureRow:
    """Build one finalized historical row with deterministic projection inputs."""
    return HistoricalFeatureRow(
        dataset_version="prepared-fixture",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
        game_id=game_id,
        game_start=start,
        team_id="CHI",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.UNKNOWN,
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
        target_box_score=BoxScoreLine(points=10),
        target_line_points=10,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def dataset(rows: tuple[HistoricalFeatureRow, ...]) -> HistoricalFeatureDataset:
    """Wrap fixture rows in one stable dataset identity."""
    return HistoricalFeatureDataset(
        dataset_version="prepared-fixture",
        feature_schema_version="1",
        generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


def fold_dataset() -> HistoricalFeatureDataset:
    """Build warmup plus two time-disjoint two-player fold batches."""
    rows = tuple(
        row(game_id, player_id, start)
        for game_id, start in (
            ("warmup", datetime(2025, 1, 1, tzinfo=UTC)),
            ("game-1", datetime(2025, 1, 2, tzinfo=UTC)),
            ("game-2", datetime(2025, 1, 3, tzinfo=UTC)),
        )
        for player_id in ("player-1", "player-2")
    )
    return dataset(rows)


def fixture_folds() -> tuple[ChronologicalFold, ...]:
    """Return adjacent single-tipoff folds for continuation checks."""
    return (
        ChronologicalFold(
            "fixture-first",
            2024,
            "early",
            datetime(2025, 1, 2, tzinfo=UTC),
            datetime(2025, 1, 2, tzinfo=UTC),
        ),
        ChronologicalFold(
            "fixture-second",
            2024,
            "middle",
            datetime(2025, 1, 3, tzinfo=UTC),
            datetime(2025, 1, 3, tzinfo=UTC),
        ),
    )


class ConstantProjector:
    """Return one stable distribution for fold orchestration tests."""

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = next(
            row for row in dataset.rows if row.player_id == player_id and row.game_id == game_id
        )
        return ProjectionSnapshot(
            player_id,
            game_id,
            target.available_as_of,
            "constant-v1",
            f"constant-input-{game_id}-{player_id}",
            scoring_policy.version,
            ProjectionDistribution.from_weighted_observations(((5.0, 1), (10.0, 1), (15.0, 1))),
            (),
        )


class SequencedProjector:
    """Expose projection call order through deterministic output versions."""

    def __init__(self) -> None:
        self.projection_count = 0

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        self.projection_count += 1
        target = next(
            row for row in dataset.rows if row.player_id == player_id and row.game_id == game_id
        )
        expected = float(self.projection_count)
        return ProjectionSnapshot(
            player_id,
            game_id,
            target.available_as_of,
            "sequenced-v1",
            f"sequenced-input-{self.projection_count}",
            scoring_policy.version,
            ProjectionDistribution.from_weighted_observations(
                ((expected - 5, 1), (expected, 1), (expected + 5, 1))
            ),
            (),
        )


def test_preparation_sorts_once_and_freezes_same_tipoff_warmup_counts() -> None:
    first = datetime(2025, 1, 1, tzinfo=UTC)
    shared_tipoff = datetime(2025, 1, 2, tzinfo=UTC)
    next_season = datetime(2025, 10, 1, tzinfo=UTC)
    prepared = prepare_backtest_dataset(
        dataset(
            (
                row("p1-next-season", "p1", next_season),
                row("p1-shared-b", "p1", shared_tipoff),
                row("p2-shared", "p2", shared_tipoff),
                row("p1-first", "p1", first),
                row("p1-shared-a", "p1", shared_tipoff),
            )
        )
    )

    assert tuple(item.game_id for item in prepared.rows) == (
        "p1-first",
        "p1-shared-a",
        "p1-shared-b",
        "p2-shared",
        "p1-next-season",
    )
    assert prepared.prior_same_season_games == (0, 1, 1, 0, 0)


def test_prepared_window_uses_inclusive_bounds_and_precomputed_warmup() -> None:
    first = datetime(2025, 1, 1, tzinfo=UTC)
    shared_tipoff = datetime(2025, 1, 2, tzinfo=UTC)
    prepared = prepare_backtest_dataset(
        dataset(
            (
                row("p1-first", "p1", first),
                row("p1-shared", "p1", shared_tipoff),
                row("p2-shared", "p2", shared_tipoff),
            )
        )
    )

    window = prepared.prepare_window(
        BacktestConfig(start_at=shared_tipoff, end_at=shared_tipoff, min_prior_games=1)
    )

    assert window.start_index == 1
    assert window.stop_index == 3
    assert tuple(item.game_id for item in window.targets) == ("p1-shared",)
    assert tuple(item.game_id for item in window.target_skips) == ("p2-shared",)
    assert window.target_skips[0].reason.endswith("found 0.")


def test_point_in_time_view_excludes_same_tipoff_outcomes_and_sanitizes_target() -> None:
    first = datetime(2025, 1, 1, tzinfo=UTC)
    shared_tipoff = datetime(2025, 1, 2, tzinfo=UTC)
    prepared = prepare_backtest_dataset(
        dataset(
            (
                row("p1-first", "p1", first),
                row("p1-shared-a", "p1", shared_tipoff),
                row("p1-shared-b", "p1", shared_tipoff),
            )
        )
    )

    view = prepared.point_in_time_dataset(prepared.rows[1])

    assert len(view.rows) == 2
    assert tuple(item.game_id for item in view.rows) == ("p1-first", "p1-shared-a")
    target = view.rows[-1]
    assert target.target_minutes is None
    assert not target.target_did_play
    assert target.target_box_score == BoxScoreLine()


def test_preparation_rejects_duplicate_player_game_keys() -> None:
    original = row("duplicate", "p1", datetime(2025, 1, 1, tzinfo=UTC))

    with pytest.raises(BacktestError, match="Duplicate historical feature row"):
        prepare_backtest_dataset(dataset((original, replace(original))))


def test_validation_folds_prepare_dataset_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_count = 0

    def count_preparation(dataset: HistoricalFeatureDataset) -> PreparedBacktestDataset:
        nonlocal preparation_count
        preparation_count += 1
        return prepare_backtest_dataset(dataset)

    monkeypatch.setattr(folds_module, "prepare_backtest_dataset", count_preparation)

    run_validation_folds(
        fold_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("reference", ConstantProjector()),),
        folds=fixture_folds(),
    )

    assert preparation_count == 1


def test_validation_folds_validate_before_projecting() -> None:
    invalid_dataset = replace(fold_dataset(), generated_at=datetime(2026, 8, 13))
    projector = SequencedProjector()

    with pytest.raises(BacktestError, match="generated_at must be timezone-aware"):
        run_validation_folds(
            invalid_dataset,
            scoring_policy=POLICY,
            models=(BacktestModel("reference", projector),),
            folds=fixture_folds(),
        )

    assert projector.projection_count == 0


def test_empty_fold_reports_zero_target_progress() -> None:
    fold = ChronologicalFold(
        "fixture-empty",
        2025,
        "late",
        datetime(2026, 3, 1, tzinfo=UTC),
        datetime(2026, 3, 2, tzinfo=UTC),
    )
    events: list[ProgressEvent] = []

    results = run_validation_folds(
        fold_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("reference", ConstantProjector()),),
        folds=(fold,),
        progress=ProgressReporter(ProgressMode.DEVELOPMENT, events.append),
        progress_stage=ProgressStage.RUN_DEVELOPMENT_FOLD,
    )

    assert results[0].report.target_count == 0
    assert [event.state for event in events] == [
        ProgressState.STARTED,
        ProgressState.ADVANCED,
        ProgressState.COMPLETED,
    ]
    assert events[-1].completed == events[-1].total == 0


def test_prepared_fold_execution_matches_repeated_public_backtests() -> None:
    folds = fixture_folds()
    config = BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    prepared_results = run_validation_folds(
        fold_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("reference", SequencedProjector()),),
        folds=folds,
        config=config,
    )
    reference_models = (BacktestModel("reference", SequencedProjector()),)
    reference_reports = tuple(
        run_backtest(
            fold_dataset(),
            scoring_policy=POLICY,
            models=reference_models,
            config=replace(config, start_at=fold.start_at, end_at=fold.end_at),
        )
        for fold in folds
    )

    for prepared_result, reference_report in zip(prepared_results, reference_reports, strict=True):
        normalized_results = tuple(
            replace(result, model=reference_report.model_results[position].model)
            for position, result in enumerate(prepared_result.report.model_results)
        )
        assert replace(prepared_result.report, model_results=normalized_results) == reference_report
