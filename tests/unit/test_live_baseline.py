from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    DatasetSourceVersion,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import ProjectionBaselineError
from sleeper_manager.projections.live_baseline import (
    DirectBaselineProjectionProvider,
    HistoricalFeatureSlice,
    LiveProjectionTarget,
    ProjectionHistoryError,
)

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
TIP_OFF = NOW + timedelta(hours=3)
POLICY = ScoringPolicy(points=1, three_pointers_made=1)
SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 1, 1, tzinfo=UTC))


def _row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
    minutes: float,
    *,
    sleeper_id: str | None = None,
) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=sleeper_id,
        game_id=game_id,
        game_start=start,
        outcome_finalized_at=start + timedelta(hours=2),
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
        target_minutes=minutes,
        target_started=False,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def _slice(*rows: HistoricalFeatureRow) -> HistoricalFeatureSlice:
    return HistoricalFeatureSlice(
        dataset_version="features-v1",
        feature_schema_version="1",
        source_versions=(DatasetSourceVersion("espn", "1", ("fixture",)),),
        rows=tuple(rows),
    )


class _StaticHistory:
    def __init__(self, rows: tuple[HistoricalFeatureRow, ...]) -> None:
        self.rows = rows
        self.requested: LiveProjectionTarget | None = None

    def load(self, target: LiveProjectionTarget, *, before: datetime) -> HistoricalFeatureSlice:
        self.requested = target
        return _slice(*self.rows)


class _FailingHistory:
    def load(self, target: LiveProjectionTarget, *, before: datetime) -> HistoricalFeatureSlice:
        raise RuntimeError("history source exploded")


def _target() -> LiveProjectionTarget:
    return LiveProjectionTarget(
        sleeper_player_id="p1",
        game_id="g-next",
        game_start=TIP_OFF,
        provider_player_id="401",
    )


def _provider(rows: tuple[HistoricalFeatureRow, ...]) -> DirectBaselineProjectionProvider:
    return DirectBaselineProjectionProvider(_StaticHistory(rows))


def test_projects_snapshot_from_provider_history() -> None:
    history = _StaticHistory(
        (
            _row("old", "401", NOW - timedelta(days=3), 20, 20, sleeper_id="p1"),
            _row("recent", "401", NOW - timedelta(days=1), 10, 10, sleeper_id="p1"),
            _row("other-player", "999", NOW - timedelta(days=1), 4, 10),
        )
    )
    provider = DirectBaselineProjectionProvider(history)

    snapshot = provider.project(_target(), scoring_policy=POLICY, decision_time=NOW)

    assert snapshot is not None
    assert snapshot.player_id == "p1"
    assert snapshot.game_id == "g-next"
    assert snapshot.available_as_of == NOW
    assert snapshot.model_version.startswith("projection-baseline-v1-")
    assert history.requested == _target()


def test_future_outcome_rows_cannot_change_the_projection() -> None:
    baseline_rows = (
        _row("old", "401", NOW - timedelta(days=3), 20, 20, sleeper_id="p1"),
        _row("recent", "401", NOW - timedelta(days=1), 10, 10, sleeper_id="p1"),
    )
    poisoned = baseline_rows + (
        _row("future-leak", "401", TIP_OFF + timedelta(days=2), 99, 36, sleeper_id="p1"),
    )

    clean = _provider(baseline_rows).project(_target(), scoring_policy=POLICY, decision_time=NOW)
    leaked = _provider(poisoned).project(_target(), scoring_policy=POLICY, decision_time=NOW)

    assert clean is not None and leaked is not None
    assert clean.distribution.expected_value == leaked.distribution.expected_value
    assert clean.input_version == leaked.input_version


def test_started_games_have_no_pregame_projection() -> None:
    started_target = LiveProjectionTarget(
        sleeper_player_id="p1",
        game_id="g-live",
        game_start=NOW - timedelta(minutes=30),
    )
    assert (
        _provider((_row("old", "401", NOW - timedelta(days=3), 20, 20, sleeper_id="p1"),)).project(
            started_target, scoring_policy=POLICY, decision_time=NOW
        )
        is None
    )


def test_missing_warmup_returns_none_instead_of_raising() -> None:
    provider = _provider(())
    assert provider.project(_target(), scoring_policy=POLICY, decision_time=NOW) is None


def test_history_source_failures_are_wrapped() -> None:
    provider = DirectBaselineProjectionProvider(_FailingHistory())
    with pytest.raises(RuntimeError, match="exploded"):
        provider.project(_target(), scoring_policy=POLICY, decision_time=NOW)


def test_naive_decision_times_are_rejected() -> None:
    with pytest.raises(ProjectionHistoryError, match="timezone-aware"):
        _provider(()).project(
            _target(), scoring_policy=POLICY, decision_time=datetime(2026, 1, 7, 18)
        )


class _DriftedSchemaHistory:
    def __init__(self, rows: tuple[HistoricalFeatureRow, ...]) -> None:
        self.rows = rows

    def load(self, target: LiveProjectionTarget, *, before: datetime) -> HistoricalFeatureSlice:
        return HistoricalFeatureSlice(
            dataset_version="features-v1",
            feature_schema_version="",
            source_versions=(),
            rows=self.rows,
        )


def test_non_warmup_baseline_errors_propagate() -> None:
    provider = DirectBaselineProjectionProvider(
        _DriftedSchemaHistory(
            (_row("old", "401", NOW - timedelta(days=3), 20, 20, sleeper_id="p1"),)
        )
    )
    with pytest.raises(ProjectionBaselineError):
        provider.project(_target(), scoring_policy=POLICY, decision_time=NOW)
