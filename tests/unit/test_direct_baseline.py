from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.direct_baseline import (
    MISSING_WARMUP_REASON,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineConfig,
    ProjectionBaselineError,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 10, tzinfo=UTC))
POLICY = ScoringPolicy(
    points=1,
    three_pointers_made=1,
    technical_foul=-1,
    flagrant_foul=-2,
)


def row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
    minutes: float,
    *,
    started: bool = False,
    did_play: bool = True,
) -> HistoricalFeatureRow:
    line = BoxScoreLine(points=points, three_pointers_made=1)
    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
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
        target_started=started,
        target_did_play=did_play,
        target_box_score=line,
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def dataset(rows: tuple[HistoricalFeatureRow, ...]) -> HistoricalFeatureDataset:
    return HistoricalFeatureDataset(
        dataset_version="features-v1",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


def test_direct_baseline_projects_sleeper_points_with_versions_reasons_and_exceedance() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    history = (
        row("old", "player-1", datetime(2025, 1, 1, tzinfo=UTC), 20, 20, started=True),
        row("recent", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10),
        row("other", "player-2", datetime(2025, 1, 9, tzinfo=UTC), 4, 10),
    )
    snapshot = DirectFantasyPointBaseline().project(
        dataset(history + (target,)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
        exceed_score=12,
    )

    assert snapshot.distribution.expected_value > 10
    assert snapshot.distribution.probability_exceeding_score is not None
    assert snapshot.distribution.probability_exceeding_score == (
        snapshot.distribution.probability_of_exceeding(12)
    )
    assert snapshot.model_version.startswith("projection-baseline-v1-")
    assert snapshot.input_version.startswith("projection-input-v3-")
    assert snapshot.scoring_policy_version == POLICY.version
    assert any(reason.code == "minutes_role" for reason in snapshot.reasons)
    assert any(reason.code == "season_shrinkage" for reason in snapshot.reasons)
    deferred = [reason for reason in snapshot.reasons if reason.code.startswith("deferred_")]
    assert len(deferred) == 5
    assert all(not reason.applied for reason in deferred)


def test_direct_baseline_excludes_future_rows_from_projection() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    prior = row("prior", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10)
    future = row("future", "player-1", datetime(2025, 1, 11, tzinfo=UTC), 100, 40)

    baseline = DirectFantasyPointBaseline()
    without_future = baseline.project(
        dataset((prior, target)), player_id="player-1", game_id="target", scoring_policy=POLICY
    )
    with_future = baseline.project(
        dataset((prior, target, future)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert with_future.distribution.expected_value == without_future.distribution.expected_value


def test_legacy_target_outcomes_do_not_change_pregame_projection() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0)
    prior = row("prior", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10)
    changed_target = replace(
        target,
        target_minutes=40,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=100),
        target_line_points=100,
    )

    baseline = DirectFantasyPointBaseline()
    original = baseline.project(
        dataset((prior, target)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
    )
    changed = baseline.project(
        dataset((prior, changed_target)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert changed.distribution == original.distribution
    assert changed.input_version == original.input_version


def test_historical_wrapper_uses_provider_history_and_emits_sleeper_identity() -> None:
    prior = row("prior", "provider-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10)
    target = replace(
        row("target", "provider-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0),
        sleeper_id="sleeper-1",
    )

    snapshot = DirectFantasyPointBaseline().project(
        dataset((prior, target)),
        player_id="provider-1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert snapshot.player_id == "sleeper-1"
    assert snapshot.distribution.expected_value > 0


def test_pregame_request_excludes_same_tipoff_and_future_outcomes() -> None:
    prior = row("prior", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10)
    target_start = datetime(2025, 1, 10, 18, tzinfo=UTC)
    overlapping = row(
        "overlapping",
        "player-2",
        target_start - timedelta(minutes=30),
        100,
        40,
    )
    same_tipoff = row("same", "player-2", target_start, 100, 40)
    future = row("future", "player-1", datetime(2025, 1, 11, tzinfo=UTC), 100, 40)
    request = PregameProjectionRequest(
        dataset_version="features-v1",
        feature_schema_version="1",
        player_id="player-1",
        game_id="target",
        game_start=target_start,
        available_as_of=datetime(2025, 1, 10, 12, tzinfo=UTC),
        history=(prior, overlapping, same_tipoff, future),
    )

    snapshot = DirectFantasyPointBaseline().project_pregame(
        request,
        scoring_policy=POLICY,
    )
    reference = DirectFantasyPointBaseline().project_pregame(
        replace(request, history=(prior,)),
        scoring_policy=POLICY,
    )

    assert snapshot.distribution == reference.distribution
    assert snapshot.input_version == reference.input_version
    assert request.history == (prior,)


def test_pregame_projection_reports_stable_missing_warmup_reason() -> None:
    request = PregameProjectionRequest(
        dataset_version="features-v1",
        feature_schema_version="1",
        player_id="player-1",
        game_id="target",
        game_start=datetime(2025, 1, 10, 18, tzinfo=UTC),
        available_as_of=datetime(2025, 1, 10, 12, tzinfo=UTC),
        history=(),
    )

    with pytest.raises(ProjectionBaselineError) as error:
        DirectFantasyPointBaseline().project_pregame(request, scoring_policy=POLICY)

    assert error.value.reason_code == MISSING_WARMUP_REASON


def test_direct_baseline_requires_prior_same_season_observations() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    with pytest.raises(ProjectionBaselineError, match="No prior same-season"):
        DirectFantasyPointBaseline().project(
            dataset((target,)),
            player_id="player-1",
            game_id="target",
            scoring_policy=POLICY,
        )


def test_direct_baseline_preserves_recency_weighted_season_fallback() -> None:
    target = row("target", "new-player", datetime(2025, 1, 3, tzinfo=UTC), 0, 0)
    history = (
        row("older", "player-1", datetime(2025, 1, 1, tzinfo=UTC), 19, 20),
        row("recent", "player-2", datetime(2025, 1, 2, tzinfo=UTC), 9, 20),
    )
    snapshot = DirectFantasyPointBaseline(
        ProjectionBaselineConfig(recency_half_life_days=1)
    ).project(
        dataset(history + (target,)),
        player_id="new-player",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert snapshot.distribution.expected_value == pytest.approx(40 / 3)


def test_baseline_config_rejects_invalid_or_unknown_adjustments() -> None:
    with pytest.raises(ProjectionBaselineError):
        ProjectionBaselineConfig(recency_half_life_days=0)
    with pytest.raises(ProjectionBaselineError):
        ProjectionBaselineConfig(disabled_adjustments=("weather",))
