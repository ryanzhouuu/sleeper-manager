from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.backtesting.experiments.feature_validation import (
    ModelFeatureValidationError,
    _assert_frozen_manifest,
    _audit_dataset,
    _cumulative_suite,
    _markdown_report,
    _season_label,
    _season_label_from_start_year,
    run_model_feature_validation,
)
from sleeper_manager.backtesting.validation.models import GateResult, PromotionDecision
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.residual_candidates import ResidualFeature

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 10, tzinfo=UTC))


def _row(
    game_id: str = "g1",
    *,
    start: datetime | None = None,
    available_as_of: datetime | None = None,
) -> HistoricalFeatureRow:
    game_start = start or datetime(2025, 1, 1, tzinfo=UTC)
    cutoff = available_as_of or game_start - timedelta(minutes=30)
    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=cutoff,
        player_id="p1",
        sleeper_id=None,
        game_id=game_id,
        game_start=game_start,
        outcome_finalized_at=game_start + timedelta(hours=2),
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
        target_started=False,
        target_did_play=True,
        target_box_score=BoxScoreLine(),
        target_line_points=10,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def _dataset(*rows: HistoricalFeatureRow) -> HistoricalFeatureDataset:
    return HistoricalFeatureDataset(
        dataset_version="features-v1",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


def test_naive_experiment_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ModelFeatureValidationError, match="timezone-aware"):
        run_model_feature_validation(
            tmp_path,
            league_fixture=tmp_path / "league.json",
            now=datetime(2026, 1, 1),
        )


def test_audit_dataset_passes_cutoff_and_lineage_invariants() -> None:
    audit = _audit_dataset(_dataset(_row(), _row("g2", start=datetime(2025, 1, 2, tzinfo=UTC))))

    assert audit == {
        "decision_cutoff_exactly_30_minutes": True,
        "availability_not_after_cutoff": True,
        "source_lineage_present": True,
        "target_rows_unique": True,
        "timestamps_timezone_aware": True,
    }


def test_audit_dataset_flags_cutoff_duplicates_and_missing_lineage() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    late_observation = replace(
        _row(),
        available_as_of=start - timedelta(minutes=10),
        availability_observed_at=start,
    )
    duplicate = _row()
    missing_lineage = replace(_row("g2"), source_lineage=())
    audit = _audit_dataset(_dataset(late_observation, duplicate, missing_lineage))

    assert audit["decision_cutoff_exactly_30_minutes"] is False
    assert audit["availability_not_after_cutoff"] is False
    assert audit["source_lineage_present"] is False
    assert audit["target_rows_unique"] is False


def test_assert_frozen_manifest_rejects_unreadable_and_drifted_files(tmp_path: Path) -> None:
    path = tmp_path / "frozen.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ModelFeatureValidationError, match="unreadable"):
        _assert_frozen_manifest(path, {"a": 1})

    path.write_text('{"a": 1}', encoding="utf-8")
    _assert_frozen_manifest(path, {"a": 1})
    with pytest.raises(ModelFeatureValidationError, match="drifted"):
        _assert_frozen_manifest(path, {"a": 2})


def test_cumulative_suite_names_selected_features_in_order() -> None:
    suite = _cumulative_suite((ResidualFeature.REST, ResidualFeature.INJURY))

    assert tuple(model.name for model in suite.models[:3]) == (
        "reference",
        "direct_baseline",
        "last_game",
    )
    assert suite.candidate_names == ("cumulative_rest", "cumulative_rest_injury")


def test_season_labels_use_october_year_boundary() -> None:
    assert _season_label_from_start_year(2022) == "2022-23"
    assert _season_label(datetime(2023, 1, 15, tzinfo=UTC)) == "2022-23"
    assert _season_label(datetime(2023, 10, 1, tzinfo=UTC)) == "2023-24"


def test_markdown_report_includes_audit_and_gate_evidence() -> None:
    decision = PromotionDecision(
        candidate_model="rest",
        recommendation="hold",
        promotable=True,
        gates=(GateResult("mae", True, "improved"),),
    )
    cumulative = PromotionDecision(
        candidate_model="cumulative_rest",
        recommendation="promote",
        promotable=True,
        gates=(GateResult("mae", False, "regressed"),),
    )
    report = {
        "dataset": {
            "dataset_version": "features-v1",
            "row_count": 2,
            "game_count": 1,
        },
        "injury_archive": {
            "selected_reports": 1,
            "requested_reports": 2,
            "unresolved_identity_count": 0,
            "mapping_coverage_by_season": {"2022-23": {"resolved": 4}},
            "feature_observation_counts_by_season": {"2022-23": {"reported": 1}},
        },
        "promotion_decisions": (decision,),
        "cumulative_decisions": (cumulative,),
        "audit": {"source_lineage_present": True},
        "limitations": ("one season only",),
    }

    markdown = _markdown_report(report)

    assert markdown.startswith("# Model Feature Validation Report")
    assert "PASS — source_lineage_present" in markdown
    assert "| rest | hold | 1/1 |" in markdown
    assert "| cumulative_rest | promote | 0/1 |" in markdown
    assert "- one season only" in markdown
