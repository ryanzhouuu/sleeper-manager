from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.controls import CalibratedProjectionModel, NaiveProjectionBaseline
from sleeper_manager.backtesting.experiment_data import (
    HistoricalExperimentInputs,
    artifact_manifest,
    dataset_version_for,
    decision_cutoff,
    load_historical_experiment_inputs,
    scoring_policy_from_league_fixture,
)
from sleeper_manager.backtesting.experiment_injuries import (
    InjuryArchiveResult,
    acquire_injury_archive,
)
from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestModel,
    CohortDiagnostics,
    ProjectionModel,
)
from sleeper_manager.backtesting.validation import (
    ComponentGateConfig,
    DevelopmentDecision,
    FoldResult,
    PromotionDecision,
    block_bootstrap_mae_delta,
    cohort_comparison_across_folds,
    evaluate_component_gates,
    evaluate_development_candidate,
    evaluate_promotion,
    regular_season_folds,
    run_validation_folds,
    segment_comparisons,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    FEATURE_SCHEMA_VERSION,
    HistoricalFeatureDataset,
    build_historical_feature_dataset,
)
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.official_injury_mapping import InjuryMappingDiagnostic
from sleeper_manager.integrations.nba.official_injury_report import EASTERN_TIME
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from sleeper_manager.projections.opportunity_model import (
    InterpretableOpportunityModel,
    OpportunityModelConfig,
)
from sleeper_manager.projections.residual_candidates import (
    CachingProjectionModel,
    ResidualCandidateConfig,
    ResidualFeature,
    ResidualHistory,
    ShrunkenResidualCandidate,
)

REFERENCE_MODEL = "reference"
ISOLATED_FEATURES = (
    ResidualFeature.OPPONENT_IDENTITY,
    ResidualFeature.OPPONENT_STRENGTH,
    ResidualFeature.REST,
    ResidualFeature.TRAVEL,
    ResidualFeature.INJURY,
)
CUMULATIVE_ORDER = (
    ResidualFeature.OPPONENT_STRENGTH,
    ResidualFeature.REST,
    ResidualFeature.TRAVEL,
    ResidualFeature.INJURY,
)
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260813


class ModelFeatureValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationExperimentOutput:
    frozen_manifest_path: Path
    report_json_path: Path
    report_markdown_path: Path
    dataset_version: str
    selected_features: tuple[str, ...]
    recommendations: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _ModelSuite:
    models: tuple[BacktestModel, ...]
    candidate_names: tuple[str, ...]


def run_model_feature_validation(
    workspace: Path,
    *,
    league_fixture: Path,
    now: datetime | None = None,
) -> ValidationExperimentOutput:
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise ModelFeatureValidationError("Experiment timestamp must be timezone-aware")
    raw_dir = workspace / "raw"
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    scoring_policy = scoring_policy_from_league_fixture(league_fixture)
    inputs = load_historical_experiment_inputs(raw_dir, retrieved_at=generated_at)
    injuries = acquire_injury_archive(
        inputs.games,
        inputs.provider_players,
        workspace / "injuries",
        retrieved_at=generated_at,
        historical_player_ids_by_date_team=_historical_player_ids_by_date_team(inputs),
    )
    dataset = _build_dataset(inputs, injuries, scoring_policy, generated_at)
    audit = _audit_dataset(dataset)
    config = BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    folds = regular_season_folds()
    development_folds = tuple(fold for fold in folds if not fold.holdout)
    holdout_folds = tuple(fold for fold in folds if fold.holdout)

    isolated_suite = _isolated_suite()
    development_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=isolated_suite.models,
        folds=development_folds,
        config=config,
        reference_model=REFERENCE_MODEL,
    )
    development_decisions = tuple(
        evaluate_development_candidate(
            development_results,
            reference_model=REFERENCE_MODEL,
            candidate_model=feature.value,
            promotion_eligible=feature is not ResidualFeature.OPPONENT_IDENTITY,
            bootstrap_samples=BOOTSTRAP_SAMPLES,
            bootstrap_seed=BOOTSTRAP_SEED,
        )
        for feature in ISOLATED_FEATURES
    )
    selected_features = tuple(
        feature
        for feature in CUMULATIVE_ORDER
        if next(
            decision
            for decision in development_decisions
            if decision.candidate_model == feature.value
        ).selected
    )
    frozen_manifest = _frozen_manifest(
        generated_at=generated_at,
        inputs=inputs,
        injuries=injuries,
        dataset=dataset,
        scoring_policy=scoring_policy,
        config=config,
        development_folds=development_folds,
        development_decisions=development_decisions,
        selected_features=selected_features,
        league_fixture=league_fixture,
    )
    frozen_manifest_path = reports_dir / "frozen-development-manifest.json"
    _write_json(frozen_manifest_path, frozen_manifest)
    _assert_frozen_manifest(frozen_manifest_path, frozen_manifest)

    holdout_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=isolated_suite.models,
        folds=holdout_folds,
        config=config,
        reference_model=REFERENCE_MODEL,
    )
    promotion_decisions = tuple(
        evaluate_promotion(
            development_results=development_results,
            holdout_results=holdout_results,
            dataset=dataset,
            reference_model=REFERENCE_MODEL,
            candidate_model=feature.value,
            promotable=feature is not ResidualFeature.OPPONENT_IDENTITY,
            audit_passed=all(audit.values()),
        )
        for feature in ISOLATED_FEATURES
    )

    cumulative_development: tuple[FoldResult, ...] = ()
    cumulative_holdout: tuple[FoldResult, ...] = ()
    cumulative_decisions: tuple[PromotionDecision, ...] = ()
    if selected_features:
        cumulative_suite = _cumulative_suite(selected_features)
        cumulative_development = run_validation_folds(
            dataset,
            scoring_policy=scoring_policy,
            models=cumulative_suite.models,
            folds=development_folds,
            config=config,
            reference_model=REFERENCE_MODEL,
        )
        _assert_frozen_manifest(frozen_manifest_path, frozen_manifest)
        cumulative_holdout = run_validation_folds(
            dataset,
            scoring_policy=scoring_policy,
            models=cumulative_suite.models,
            folds=holdout_folds,
            config=config,
            reference_model=REFERENCE_MODEL,
        )
        cumulative_decisions = tuple(
            evaluate_promotion(
                development_results=cumulative_development,
                holdout_results=cumulative_holdout,
                dataset=dataset,
                reference_model=REFERENCE_MODEL,
                candidate_model=name,
                audit_passed=all(audit.values()),
            )
            for name in cumulative_suite.candidate_names
        )

    report = _report(
        generated_at=generated_at,
        inputs=inputs,
        injuries=injuries,
        dataset=dataset,
        scoring_policy=scoring_policy,
        config=config,
        development_results=development_results,
        holdout_results=holdout_results,
        development_decisions=development_decisions,
        promotion_decisions=promotion_decisions,
        cumulative_development=cumulative_development,
        cumulative_holdout=cumulative_holdout,
        cumulative_decisions=cumulative_decisions,
        selected_features=selected_features,
        audit=audit,
        frozen_manifest_path=frozen_manifest_path,
    )
    report_json_path = reports_dir / "model-feature-validation-report.json"
    report_markdown_path = reports_dir / "model-feature-validation-report.md"
    _write_json(report_json_path, report)
    report_markdown_path.write_text(_markdown_report(report))
    recommendations = tuple(
        (decision.candidate_model, decision.recommendation)
        for decision in promotion_decisions + cumulative_decisions
    )
    return ValidationExperimentOutput(
        frozen_manifest_path,
        report_json_path,
        report_markdown_path,
        dataset.dataset_version,
        tuple(feature.value for feature in selected_features),
        recommendations,
    )


def _build_dataset(
    inputs: HistoricalExperimentInputs,
    injuries: InjuryArchiveResult,
    scoring_policy: ScoringPolicy,
    generated_at: datetime,
) -> HistoricalFeatureDataset:
    injury_hashes = tuple(
        selection.sha256 for selection in injuries.selections if selection.sha256 is not None
    )
    version = dataset_version_for(
        inputs.artifacts,
        scoring_policy=scoring_policy,
        injury_hashes=injury_hashes,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )
    return build_historical_feature_dataset(
        box_scores=inputs.player_box_scores,
        games=inputs.games,
        teams=inputs.teams,
        player_mappings=(),
        injury_reports=injuries.snapshots,
        availability=injuries.availability,
        decision_cutoffs=decision_cutoff,
        dataset_version=version,
        generated_at=generated_at,
        team_box_scores=inputs.team_box_scores,
    )


def _historical_player_ids_by_date_team(
    inputs: HistoricalExperimentInputs,
) -> dict[tuple[date, str], frozenset[str]]:
    team_abbreviations = {
        team.provider_id: normalize_team(team.abbreviation) for team in inputs.teams
    }
    game_dates = {
        game.provider_id: game.start_time.astimezone(EASTERN_TIME).date() for game in inputs.games
    }
    index: dict[tuple[date, str], set[str]] = {}
    for box_score in inputs.player_box_scores:
        game_date = game_dates.get(box_score.game_id)
        team = team_abbreviations.get(box_score.team_id)
        if game_date is None or team is None:
            continue
        index.setdefault((game_date, team), set()).add(box_score.player_id)
    return {key: frozenset(player_ids) for key, player_ids in index.items()}


def _isolated_suite() -> _ModelSuite:
    reference = CachingProjectionModel(NaiveProjectionBaseline("season_average"), max_entries=4096)
    residual_history = ResidualHistory(reference)
    models = (
        BacktestModel(REFERENCE_MODEL, _calibrated(reference)),
        BacktestModel(
            "direct_baseline",
            _calibrated(CachingProjectionModel(DirectFantasyPointBaseline(), max_entries=4096)),
        ),
        BacktestModel(
            "last_game",
            _calibrated(
                CachingProjectionModel(NaiveProjectionBaseline("last_game"), max_entries=4096)
            ),
        ),
    ) + tuple(
        BacktestModel(
            feature.value,
            _calibrated(
                ShrunkenResidualCandidate(
                    ResidualCandidateConfig((feature,)),
                    reference=reference,
                    residual_history=residual_history,
                )
            ),
        )
        for feature in ISOLATED_FEATURES
    )
    return _ModelSuite(models, tuple(feature.value for feature in ISOLATED_FEATURES))


def _cumulative_suite(features: tuple[ResidualFeature, ...]) -> _ModelSuite:
    reference = CachingProjectionModel(NaiveProjectionBaseline("season_average"), max_entries=4096)
    residual_history = ResidualHistory(reference)
    cumulative: list[ResidualFeature] = []
    models: list[BacktestModel] = [
        BacktestModel(REFERENCE_MODEL, _calibrated(reference)),
        BacktestModel(
            "direct_baseline",
            _calibrated(CachingProjectionModel(DirectFantasyPointBaseline(), max_entries=4096)),
        ),
        BacktestModel(
            "last_game",
            _calibrated(
                CachingProjectionModel(NaiveProjectionBaseline("last_game"), max_entries=4096)
            ),
        ),
    ]
    names: list[str] = []
    for feature in features:
        cumulative.append(feature)
        name = "cumulative_" + "_".join(value.value for value in cumulative)
        models.append(
            BacktestModel(
                name,
                _calibrated(
                    ShrunkenResidualCandidate(
                        ResidualCandidateConfig(tuple(cumulative)),
                        reference=reference,
                        residual_history=residual_history,
                    )
                ),
            )
        )
        names.append(name)
    return _ModelSuite(tuple(models), tuple(names))


def _calibrated(model: ProjectionModel) -> CachingProjectionModel:
    return CachingProjectionModel(CalibratedProjectionModel(model), max_entries=4096)


def _audit_dataset(dataset: HistoricalFeatureDataset) -> dict[str, bool]:
    return {
        "decision_cutoff_exactly_30_minutes": all(
            (row.game_start - row.available_as_of).total_seconds() == 1800 for row in dataset.rows
        ),
        "availability_not_after_cutoff": all(
            row.availability_observed_at is None
            or row.availability_observed_at <= row.available_as_of
            for row in dataset.rows
        ),
        "source_lineage_present": all(row.source_lineage for row in dataset.rows),
        "target_rows_unique": len({(row.player_id, row.game_id) for row in dataset.rows})
        == len(dataset.rows),
        "timestamps_timezone_aware": all(
            row.game_start.tzinfo is not None and row.available_as_of.tzinfo is not None
            for row in dataset.rows
        ),
    }


def _frozen_manifest(
    *,
    generated_at: datetime,
    inputs: HistoricalExperimentInputs,
    injuries: InjuryArchiveResult,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    config: BacktestConfig,
    development_folds: Iterable[Any],
    development_decisions: Iterable[DevelopmentDecision],
    selected_features: Iterable[ResidualFeature],
    league_fixture: Path,
) -> dict[str, Any]:
    return {
        "manifest_version": "model-feature-validation-v2",
        "frozen_at": generated_at,
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "target_semantics": "reconstructed_sleeper_policy",
        "league_fixture": str(league_fixture),
        "scoring_policy_version": scoring_policy.version,
        "source_artifacts": artifact_manifest(inputs.artifacts),
        "injury_report_selections": injuries.selections,
        "folds": tuple(development_folds),
        "backtest_config_version": config.version,
        "thresholds": config.thresholds,
        "intervals": config.intervals,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "candidate_config": _candidate_configuration(),
        "reference_model": REFERENCE_MODEL,
        "diagnostic_models": ("direct_baseline", "last_game"),
        "calibration": _calibration_configuration(),
        "development_decisions": tuple(development_decisions),
        "selected_cumulative_features": tuple(feature.value for feature in selected_features),
        "interaction_candidates": (),
    }


def _candidate_configuration() -> dict[str, Any]:
    return {
        "model": "shrunken prior-baseline residual group adjustment",
        "recency_half_life_days": 60.0,
        "shrinkage_games": 20.0,
        "max_adjustment": 10.0,
        "lookback_days": 365,
        "opponent_strength_lookback_games": 10,
        "opponent_strength_shrinkage_games": 5.0,
        "fallbacks": {
            "opponent": "contemporaneous league average or explicit unknown band",
            "rest": "unknown",
            "travel": "no_prior_game or unknown_venue",
            "injury": "missing_report, team_not_yet_submitted, or not_listed",
        },
    }


def _calibration_configuration() -> dict[str, Any]:
    return {
        "model": "rolling empirical residual distribution",
        "minimum_samples": 64,
        "maximum_samples": 4096,
        "refresh_interval": 256,
        "point_in_time": True,
    }


def _report(
    *,
    generated_at: datetime,
    inputs: HistoricalExperimentInputs,
    injuries: InjuryArchiveResult,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    config: BacktestConfig,
    development_results: tuple[FoldResult, ...],
    holdout_results: tuple[FoldResult, ...],
    development_decisions: tuple[DevelopmentDecision, ...],
    promotion_decisions: tuple[PromotionDecision, ...],
    cumulative_development: tuple[FoldResult, ...],
    cumulative_holdout: tuple[FoldResult, ...],
    cumulative_decisions: tuple[PromotionDecision, ...],
    selected_features: tuple[ResidualFeature, ...],
    audit: Mapping[str, bool],
    frozen_manifest_path: Path,
) -> dict[str, Any]:
    candidate_names = tuple(feature.value for feature in ISOLATED_FEATURES)
    diagnostics = {
        candidate: {
            "development_bootstrap": block_bootstrap_mae_delta(
                development_results,
                reference_model=REFERENCE_MODEL,
                candidate_model=candidate,
                samples=BOOTSTRAP_SAMPLES,
                seed=BOOTSTRAP_SEED,
            ),
            "holdout_segments": segment_comparisons(
                holdout_results,
                dataset=dataset,
                reference_model=REFERENCE_MODEL,
                candidate_model=candidate,
            ),
        }
        for candidate in candidate_names
    }
    injury_observations = Counter(row.availability_observation.value for row in dataset.rows)
    injury_observations_by_season: dict[str, Counter[str]] = {}
    for row in dataset.rows:
        season = _season_label(row.game_start)
        injury_observations_by_season.setdefault(season, Counter())[
            row.availability_observation.value
        ] += 1
    mapping_diagnostics = _injury_mapping_diagnostics(injuries.mapping_diagnostics)
    return {
        "report_version": "model-feature-validation-v2",
        "generated_at": generated_at,
        "dataset": {
            "dataset_version": dataset.dataset_version,
            "feature_schema_version": dataset.feature_schema_version,
            "row_count": len(dataset.rows),
            "game_count": len({row.game_id for row in dataset.rows}),
            "season_count": 4,
            "source_versions": dataset.source_versions,
            "excluded_source_player_rows": inputs.excluded_player_rows,
        },
        "target": {
            "semantics": "reconstructed_sleeper_policy",
            "scoring_policy_version": scoring_policy.version,
            "decision_cutoff_minutes": 30,
        },
        "injury_archive": {
            "requested_reports": len(injuries.selections),
            "selected_reports": sum(value.selected_at is not None for value in injuries.selections),
            "missing_reports": sum(value.selected_at is None for value in injuries.selections),
            "unique_snapshots": len(injuries.snapshots),
            "unresolved_identity_count": injuries.unresolved_identity_count,
            "mapping_warning_count": injuries.mapping_warning_count,
            **mapping_diagnostics,
            "fallback_selections": sum(
                value.selected_at is not None and value.selected_at != value.requested_at
                for value in injuries.selections
            ),
            "unavailable_archive_objects": sum(
                len(value.unavailable_candidates) for value in injuries.selections
            ),
            "feature_observation_counts": dict(sorted(injury_observations.items())),
            "feature_observation_counts_by_season": {
                season: dict(sorted(counts.items()))
                for season, counts in sorted(injury_observations_by_season.items())
            },
        },
        "configuration": {
            "backtest_config_version": config.version,
            "thresholds": config.thresholds,
            "intervals": config.intervals,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "reference_model": REFERENCE_MODEL,
            "diagnostic_models": ("direct_baseline", "last_game"),
            "candidate": _candidate_configuration(),
            "calibration": _calibration_configuration(),
        },
        "frozen_manifest_path": str(frozen_manifest_path),
        "selected_cumulative_features": tuple(feature.value for feature in selected_features),
        "development_selection": development_decisions,
        "promotion_decisions": promotion_decisions,
        "cumulative_decisions": cumulative_decisions,
        "development_folds": tuple(_fold_summary(result) for result in development_results),
        "holdout_folds": tuple(_fold_summary(result) for result in holdout_results),
        "cumulative_development_folds": tuple(
            _fold_summary(result) for result in cumulative_development
        ),
        "cumulative_holdout_folds": tuple(_fold_summary(result) for result in cumulative_holdout),
        "diagnostics": diagnostics,
        "audit": dict(audit),
        "limitations": (
            "Fantasy points are reconstructed from NBA box scores under the configured "
            "Sleeper policy rather than read from a historical Sleeper total.",
            "Official injury identities use normalized player name and team matching; unresolved "
            "entries remain counted and are not imputed.",
            "Opponent identity is diagnostic and ineligible for production promotion.",
            "No interaction was tested unless both parent families passed the frozen "
            "development gate.",
        ),
    }


def _fold_summary(result: FoldResult) -> dict[str, Any]:
    skip_reasons = Counter(skip.reason for skip in result.report.target_skips)
    models: dict[str, Any] = {}
    for model_result in result.report.model_results:
        model_skip_reasons = Counter(skip.reason for skip in model_result.skips)
        models[model_result.model.name] = {
            "metrics": model_result.metrics,
            "model_versions": sorted(
                {observation.model_version for observation in model_result.observations}
            ),
            "input_version_count": len(
                {observation.input_version for observation in model_result.observations}
            ),
            "skip_reasons": dict(sorted(model_skip_reasons.items())),
        }
    return {
        "fold": result.fold,
        "target_count": result.report.target_count,
        "target_skip_reasons": dict(sorted(skip_reasons.items())),
        "models": models,
        "comparisons": result.report.comparisons,
    }


def _injury_mapping_diagnostics(
    diagnostics: Iterable[InjuryMappingDiagnostic],
) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    by_season: dict[str, Counter[str]] = {}
    by_season_team: dict[str, dict[str, Counter[str]]] = {}
    unresolved_names: Counter[tuple[str, str, str]] = Counter()
    for diagnostic in diagnostics:
        category = diagnostic.category.value
        season = _season_label_from_start_year(diagnostic.season)
        category_counts[category] += diagnostic.count
        by_season.setdefault(season, Counter())[category] += diagnostic.count
        by_season_team.setdefault(season, {}).setdefault(diagnostic.team_abbreviation, Counter())[
            category
        ] += diagnostic.count
        if category not in {
            "resolved",
            "resolved_name_only",
            "resolved_partial_name_team",
            "resolved_subset_name_team",
            "resolved_historical_name_team",
        }:
            unresolved_names[
                (season, diagnostic.team_abbreviation, diagnostic.normalized_name)
            ] += diagnostic.count
    return {
        "mapping_category_counts": dict(sorted(category_counts.items())),
        "mapping_coverage_by_season": {
            season: dict(sorted(counts.items())) for season, counts in sorted(by_season.items())
        },
        "mapping_coverage_by_season_team": {
            season: {
                team: dict(sorted(counts.items())) for team, counts in sorted(team_counts.items())
            }
            for season, team_counts in sorted(by_season_team.items())
        },
        "unresolved_name_team_examples": [
            {
                "season": season,
                "team_abbreviation": team,
                "normalized_name": name,
                "count": count,
            }
            for (season, team, name), count in unresolved_names.most_common(25)
        ],
    }


def _season_label(value: datetime) -> str:
    start_year = value.year if value.month >= 10 else value.year - 1
    return _season_label_from_start_year(start_year)


def _season_label_from_start_year(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def _markdown_report(report: Mapping[str, Any]) -> str:
    dataset = report["dataset"]
    injury = report["injury_archive"]
    decisions = report["promotion_decisions"]
    lines = [
        "# Model Feature Validation Report",
        "",
        f"Dataset: `{dataset['dataset_version']}` ({dataset['row_count']} player-games, "
        f"{dataset['game_count']} NBA games).",
        "",
        "Target: reconstructed Sleeper-policy fantasy points at a 30-minute pre-tipoff cutoff.",
        "",
        f"Injury archive: {injury['selected_reports']}/{injury['requested_reports']} cutoff "
        f"reports selected; {injury['unresolved_identity_count']} report identities unresolved.",
        "",
        "Reference: season-average baseline with rolling point-in-time residual calibration; "
        "direct baseline and last-game remain diagnostics.",
        "",
        "## Injury data quality",
        "",
        "| Season | Team-confirmed | Name-only | Partial team | Subset team | Historical date | "
        "No name/team match | "
        "Ambiguous team | Ambiguous name | Ambiguous partial |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for season, counts in injury["mapping_coverage_by_season"].items():
        lines.append(
            f"| {season} | {counts.get('resolved', 0)} | "
            f"{counts.get('resolved_name_only', 0)} | "
            f"{counts.get('resolved_partial_name_team', 0)} | "
            f"{counts.get('resolved_subset_name_team', 0)} | "
            f"{counts.get('resolved_historical_name_team', 0)} | "
            f"{counts.get('no_name_team_match', 0)} | "
            f"{counts.get('ambiguous_name_team_match', 0)} | "
            f"{counts.get('ambiguous_name_only', 0)} | "
            f"{counts.get('ambiguous_partial_name_team', 0)} |"
        )
    lines.extend(
        [
            "",
            "| Season | Reported | Not listed | Team not yet submitted | Missing report |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for season, counts in injury["feature_observation_counts_by_season"].items():
        lines.append(
            f"| {season} | {counts.get('reported', 0)} | {counts.get('not_listed', 0)} | "
            f"{counts.get('team_not_yet_submitted', 0)} | {counts.get('missing_report', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Promotion recommendations",
            "",
            "| Candidate | Recommendation | Passed gates |",
            "| --- | --- | ---: |",
        ]
    )
    for decision in decisions:
        passed = sum(gate.passed for gate in decision.gates)
        lines.append(
            f"| {decision.candidate_model} | {decision.recommendation} | "
            f"{passed}/{len(decision.gates)} |"
        )
    if report["cumulative_decisions"]:
        for decision in report["cumulative_decisions"]:
            passed = sum(gate.passed for gate in decision.gates)
            lines.append(
                f"| {decision.candidate_model} | {decision.recommendation} | "
                f"{passed}/{len(decision.gates)} |"
            )
    lines.extend(["", "## Gate evidence", ""])
    for decision in tuple(decisions) + tuple(report["cumulative_decisions"]):
        lines.append(f"### {decision.candidate_model}")
        lines.append("")
        for gate in decision.gates:
            marker = "PASS" if gate.passed else "FAIL"
            lines.append(f"- {marker} — {gate.name}: {gate.evidence}")
        lines.append("")
    lines.extend(["## Audit", ""])
    for name, passed in report["audit"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — {name}")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_json_value(value), indent=2, sort_keys=True) + "\n")


def _assert_frozen_manifest(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        actual = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ModelFeatureValidationError("Frozen development manifest is unreadable") from error
    if actual != _json_value(expected):
        raise ModelFeatureValidationError("Frozen development manifest drifted before holdout")


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


# --- Phase 4 validation closure ---------------------------------------------------------
#
# A frozen, interpretable-opportunity-model-centered comparison, separate from the older
# residual-feature-selection experiment above. That older experiment (and its
# `frozen-development-manifest.json` / `model-feature-validation-report.json` artifacts) remain
# available as historical evidence but are not the Phase 4 selection report.

PHASE4_DIRECT_BASELINE = "direct_baseline"
PHASE4_SEASON_AVERAGE = "season_average"
PHASE4_LAST_GAME = "last_game"
PHASE4_OPPORTUNITY_FULL = "opportunity_full"
PHASE4_OPPORTUNITY_NO_PACE = "opportunity_no_pace"
PHASE4_OPPORTUNITY_NO_DEFENSE = "opportunity_no_defense"
PHASE4_RAW_SUITE_NAMES: tuple[str, ...] = (
    PHASE4_DIRECT_BASELINE,
    PHASE4_SEASON_AVERAGE,
    PHASE4_LAST_GAME,
    PHASE4_OPPORTUNITY_FULL,
    PHASE4_OPPORTUNITY_NO_PACE,
    PHASE4_OPPORTUNITY_NO_DEFENSE,
)
PHASE4_CALIBRATED_DIRECT = "direct_baseline_calibrated"
PHASE4_CALIBRATED_OPPORTUNITY = "opportunity_full_calibrated"
PHASE4_SECONDARY_SUITE_NAMES: tuple[str, ...] = (
    PHASE4_CALIBRATED_DIRECT,
    PHASE4_CALIBRATED_OPPORTUNITY,
)
PHASE4_SELECTION_COHORT = "top_108"
PHASE4_MAX_SELECTION_MAE_DELTA = 0.0
PHASE4_MIN_TOP_180_COVERAGE = 0.98
PHASE4_INTERVAL_TOLERANCE = 0.05


class Phase4ValidationError(RuntimeError):
    pass


def phase4_raw_suite(
    *, opportunity_config: OpportunityModelConfig | None = None
) -> tuple[BacktestModel, ...]:
    """The frozen raw comparison. Names and order are deterministic and never reused for a
    different candidate roster -- ablations exist to explain the selected model, not to reopen
    feature search."""
    base_config = opportunity_config or OpportunityModelConfig()
    no_pace_config = replace(base_config, disable_pace=True)
    no_defense_config = replace(base_config, disable_defense=True)
    return (
        BacktestModel(PHASE4_DIRECT_BASELINE, DirectFantasyPointBaseline()),
        BacktestModel(PHASE4_SEASON_AVERAGE, NaiveProjectionBaseline("season_average")),
        BacktestModel(PHASE4_LAST_GAME, NaiveProjectionBaseline("last_game")),
        BacktestModel(PHASE4_OPPORTUNITY_FULL, InterpretableOpportunityModel(base_config)),
        BacktestModel(PHASE4_OPPORTUNITY_NO_PACE, InterpretableOpportunityModel(no_pace_config)),
        BacktestModel(
            PHASE4_OPPORTUNITY_NO_DEFENSE, InterpretableOpportunityModel(no_defense_config)
        ),
    )


def phase4_secondary_calibrated_suite(
    *, opportunity_config: OpportunityModelConfig | None = None
) -> tuple[BacktestModel, ...]:
    """Identically-configured rolling residual-calibrated variants of the direct baseline and
    the full opportunity model, as secondary diagnostics only. No calibrated ablations -- a
    calibrated result can never override a failed raw-distribution gate."""
    base_config = opportunity_config or OpportunityModelConfig()
    return (
        BacktestModel(
            PHASE4_CALIBRATED_DIRECT,
            CachingProjectionModel(
                CalibratedProjectionModel(DirectFantasyPointBaseline()), max_entries=4096
            ),
        ),
        BacktestModel(
            PHASE4_CALIBRATED_OPPORTUNITY,
            CachingProjectionModel(
                CalibratedProjectionModel(InterpretableOpportunityModel(base_config)),
                max_entries=4096,
            ),
        ),
    )


def phase4_frozen_manifest(
    *,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    cohort_config: CohortConfig,
    backtest_config: BacktestConfig,
    component_gate: ComponentGateConfig,
    raw_suite: tuple[BacktestModel, ...],
    secondary_suite: tuple[BacktestModel, ...],
) -> dict[str, Any]:
    """Freeze candidate, cohort, metric, gate, and index configuration together.

    The dataset/scoring-policy/cohort-config/backtest-config versions and each model's own
    version already fully determine the deterministic index behavior in
    `opportunity_model.py` and `cohorts.py` -- no separate free-standing "index config" exists.
    """
    return {
        "manifest_version": "phase4-validation-closure-v1",
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "scoring_policy_version": scoring_policy.version,
        "cohort_config_version": cohort_config.version,
        "backtest_config_version": backtest_config.version,
        "component_gate": {
            "max_regression_fraction": component_gate.max_regression_fraction,
            "min_calibration_bin_size": component_gate.min_calibration_bin_size,
            "calibration_tolerance": component_gate.calibration_tolerance,
        },
        "raw_suite": tuple((model.name, _model_version(model)) for model in raw_suite),
        "secondary_suite": tuple((model.name, _model_version(model)) for model in secondary_suite),
        "selection_rule": {
            "reference_model": PHASE4_DIRECT_BASELINE,
            "candidate_model": PHASE4_OPPORTUNITY_FULL,
            "selection_cohort": PHASE4_SELECTION_COHORT,
            "max_mae_delta": PHASE4_MAX_SELECTION_MAE_DELTA,
            "min_top_180_coverage": PHASE4_MIN_TOP_180_COVERAGE,
            "interval_tolerance": PHASE4_INTERVAL_TOLERANCE,
        },
    }


def _model_version(model: BacktestModel) -> str:
    return str(getattr(model.projector, "model_version", type(model.projector).__name__))


def freeze_phase4_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, manifest)


def assert_phase4_manifest_frozen(path: Path, expected: Mapping[str, Any]) -> None:
    """Refuse locked-retrospective evaluation on a missing or mismatched frozen manifest."""
    if not path.exists():
        raise Phase4ValidationError(
            f"Locked retrospective evaluation requires a frozen manifest at {path!s}, "
            "but none has been written"
        )
    try:
        actual = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Phase4ValidationError(f"Frozen Phase 4 manifest at {path!s} is unreadable") from error
    if actual != _json_value(expected):
        raise Phase4ValidationError(
            "Frozen Phase 4 manifest does not match the current source revision and "
            "configuration; locked retrospective evaluation is refused"
        )


@dataclass(frozen=True, slots=True)
class Phase4SelectionDecision:
    selected_model: str
    reference_model: str
    candidate_model: str
    cohort: str
    mae_delta: float | None
    common_sample_count: int
    rule_passed: bool
    coverage_gate_passed: bool
    interval_gate_passed: bool
    component_gate_passed: bool
    invariants_passed: bool
    provisional: bool
    evidence: str


def evaluate_phase4_selection(
    locked_retrospective_results: tuple[FoldResult, ...],
    *,
    backtest_config: BacktestConfig,
    component_gate_config: ComponentGateConfig | None = None,
) -> Phase4SelectionDecision:
    """Apply the strict, mechanical Phase 4 selection rule to locked-retrospective evidence.

    The candidate advances only when every gate passes in every given fold -- coverage,
    interval calibration, and component gates are each required to hold fold-by-fold (a
    stricter bar than pooling across folds, and it avoids merging per-fold calibration bins).
    The top-108 MAE meet-or-beat comparison is the one gate pooled across folds on common
    successful observations, since it is the frozen headline selection number.

    ``backtest_config`` must be the exact config used to produce ``locked_retrospective_results``
    (e.g. via ``run_validation_folds``) -- exceedance probabilities are baked into each
    observation at that config's thresholds, and a mismatched config fails closed with a
    ``BacktestError`` rather than silently returning a comparison for the wrong thresholds.
    """
    if not locked_retrospective_results:
        raise Phase4ValidationError("Locked retrospective selection requires at least one fold")
    gate_config = component_gate_config or ComponentGateConfig()
    comparison = cohort_comparison_across_folds(
        locked_retrospective_results,
        reference_model=PHASE4_DIRECT_BASELINE,
        candidate_model=PHASE4_OPPORTUNITY_FULL,
        cohort=PHASE4_SELECTION_COHORT,
        config=backtest_config,
    )
    rule_passed = (
        comparison.mae_delta is not None and comparison.mae_delta <= PHASE4_MAX_SELECTION_MAE_DELTA
    )
    coverage_gate_passed = all(
        _cohort_diagnostic(fold_result, PHASE4_OPPORTUNITY_FULL, "top_180").coverage
        >= PHASE4_MIN_TOP_180_COVERAGE
        for fold_result in locked_retrospective_results
    )
    interval_gate_passed = all(
        _interval_within_tolerance(fold_result, PHASE4_OPPORTUNITY_FULL)
        for fold_result in locked_retrospective_results
    )
    component_gate_passed = all(
        gate.passed
        for fold_result in locked_retrospective_results
        for gate in evaluate_component_gates(
            _cohort_diagnostic(fold_result, PHASE4_OPPORTUNITY_FULL, "top_180"), config=gate_config
        )
    )
    # run_backtest raises BacktestError before any report is produced if a cohort-invariance,
    # point-in-time, or lineage violation occurs -- reaching this point already proves the
    # invariant held for every fold evaluated.
    invariants_passed = True
    selected = (
        PHASE4_OPPORTUNITY_FULL
        if (
            rule_passed
            and coverage_gate_passed
            and interval_gate_passed
            and component_gate_passed
            and invariants_passed
        )
        else PHASE4_DIRECT_BASELINE
    )
    return Phase4SelectionDecision(
        selected_model=selected,
        reference_model=PHASE4_DIRECT_BASELINE,
        candidate_model=PHASE4_OPPORTUNITY_FULL,
        cohort=PHASE4_SELECTION_COHORT,
        mae_delta=comparison.mae_delta,
        common_sample_count=comparison.common_sample_count,
        rule_passed=rule_passed,
        coverage_gate_passed=coverage_gate_passed,
        interval_gate_passed=interval_gate_passed,
        component_gate_passed=component_gate_passed,
        invariants_passed=invariants_passed,
        provisional=True,
        evidence=(
            f"top-108 MAE delta={comparison.mae_delta} over {comparison.common_sample_count} "
            "common successful observations, pooled across all locked-retrospective folds. "
            "season_average and last_game remain audit controls, never selection fallbacks. "
            "Advancing means eligible for policy validation, not approved for production; "
            "the decision is provisional pending Phases 6 and 7."
        ),
    )


def _cohort_diagnostic(fold_result: FoldResult, model_name: str, cohort: str) -> CohortDiagnostics:
    return next(
        diagnostic
        for diagnostic in fold_result.report.result_for(model_name).cohort_diagnostics
        if diagnostic.cohort == cohort
    )


def _interval_within_tolerance(fold_result: FoldResult, model_name: str) -> bool:
    diagnostics = _cohort_diagnostic(fold_result, model_name, "top_180")
    for interval in diagnostics.full_mixture.intervals:
        if interval.observed_coverage is None:
            return False
        if abs(interval.observed_coverage - interval.nominal_coverage) > PHASE4_INTERVAL_TOLERANCE:
            return False
    return True


def phase4_report(
    *,
    generated_at: datetime,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    backtest_config: BacktestConfig,
    component_gate_config: ComponentGateConfig,
    development_results: tuple[FoldResult, ...],
    locked_retrospective_results: tuple[FoldResult, ...],
) -> dict[str, Any]:
    """Build the complete Phase 4 report.

    ``generated_at`` is the only field that varies between identical cached reruns; every other
    field lives under ``modeled`` so two reports can be compared for byte-equivalence by
    comparing ``modeled`` alone.
    """
    decision = evaluate_phase4_selection(
        locked_retrospective_results,
        backtest_config=backtest_config,
        component_gate_config=component_gate_config,
    )
    model_names = (
        tuple(result.model.name for result in locked_retrospective_results[0].report.model_results)
        if locked_retrospective_results
        else ()
    )
    return {
        "report_version": "phase4-validation-closure-v1",
        "generated_at": generated_at,
        "modeled": {
            "locked_evaluation": {
                "label": "locked_retrospective",
                "note": (
                    "2025-26 legacy results were previously inspected during model "
                    "development; this is not untouched holdout evidence. 2026-27 live shadow "
                    "data remains the honest out-of-time evaluation required before "
                    "operational reliance."
                ),
            },
            "selection": decision,
            "manifest_path": str(manifest_path),
            "manifest": dict(manifest),
            "dataset": {
                "dataset_version": dataset.dataset_version,
                "feature_schema_version": dataset.feature_schema_version,
                "source_versions": dataset.source_versions,
            },
            "scoring_policy_version": scoring_policy.version,
            "model_names": model_names,
            "development_folds": tuple(
                _phase4_fold_summary(result) for result in development_results
            ),
            "locked_retrospective_folds": tuple(
                _phase4_fold_summary(result) for result in locked_retrospective_results
            ),
            "limitations": (
                "The selection is provisional pending Phases 6 and 7 team-week replay "
                "evidence; MAE alone does not promote a production policy.",
                "season_average and last_game are audit controls only, never selection fallbacks.",
                "Coverage, interval, and component gates are each required to pass in every "
                "locked retrospective fold; a single failing fold fails that gate.",
            ),
        },
    }


def _phase4_fold_summary(result: FoldResult) -> dict[str, Any]:
    fold = result.fold
    target_skip_reasons = Counter(skip.reason for skip in result.report.target_skips)
    models: dict[str, Any] = {}
    for model_result in result.report.model_results:
        models[model_result.model.name] = {
            "metrics": model_result.metrics,
            "cohort_diagnostics": {
                diagnostic.cohort: diagnostic for diagnostic in model_result.cohort_diagnostics
            },
            "skip_reasons": dict(Counter(skip.reason for skip in model_result.skips)),
        }
    return {
        "fold_name": fold.name,
        "season_start": fold.season_start,
        "phase": fold.phase,
        "evidence_label": "locked_retrospective" if fold.holdout else "development",
        "target_count": result.report.target_count,
        "target_skip_reasons": dict(sorted(target_skip_reasons.items())),
        "models": models,
    }


def phase4_markdown_report(report: Mapping[str, Any]) -> str:
    modeled = report["modeled"]
    decision: Phase4SelectionDecision = modeled["selection"]
    lines = [
        "# Phase 4 Validation Closure Report",
        "",
        f"**Selected baseline:** `{decision.selected_model}` "
        "(provisional -- pending Phases 6 and 7 team-week replay).",
        "",
        f"Locked evaluation label: `{modeled['locked_evaluation']['label']}`. "
        f"{modeled['locked_evaluation']['note']}",
        "",
        "## Selection decision",
        "",
        "| Gate | Passed |",
        "| --- | --- |",
        f"| Strict top-108 MAE meet-or-beat | {decision.rule_passed} |",
        f"| Top-180 coverage >= 98% | {decision.coverage_gate_passed} |",
        f"| Interval calibration within 5pp of nominal | {decision.interval_gate_passed} |",
        f"| Component non-regression and calibration | {decision.component_gate_passed} |",
        f"| Point-in-time, scoring, and cohort invariants | {decision.invariants_passed} |",
        "",
        f"Top-108 MAE delta (`{decision.candidate_model}` minus `{decision.reference_model}`): "
        f"`{decision.mae_delta}` over {decision.common_sample_count} common successful "
        "observations.",
        "",
        "## Locked retrospective folds",
        "",
        "| Fold | Evidence | Targets | Target skip reasons |",
        "| --- | --- | ---: | --- |",
    ]
    for fold_summary in modeled["locked_retrospective_folds"]:
        lines.append(
            f"| {fold_summary['fold_name']} | {fold_summary['evidence_label']} | "
            f"{fold_summary['target_count']} | {fold_summary['target_skip_reasons']} |"
        )
    lines.extend(
        [
            "",
            f"## Cohort coverage and full-mixture metrics (`{decision.candidate_model}`)",
            "",
            "| Fold | Cohort | Target count | Successful | Coverage | MAE |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for fold_summary in modeled["locked_retrospective_folds"]:
        candidate = fold_summary["models"].get(decision.candidate_model)
        if candidate is None:
            continue
        for cohort_name, diagnostic in candidate["cohort_diagnostics"].items():
            lines.append(
                f"| {fold_summary['fold_name']} | {cohort_name} | {diagnostic.target_count} | "
                f"{diagnostic.successful_count} | {diagnostic.coverage} | "
                f"{diagnostic.full_mixture.mae} |"
            )
    lines.extend(
        [
            "",
            "## Secondary calibrated diagnostics",
            "",
            "These diagnostics cannot override a failed raw-distribution selection gate.",
            "",
            "| Fold | Model | Cohort | Coverage | MAE |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for fold_summary in modeled["locked_retrospective_folds"]:
        for model_name in PHASE4_SECONDARY_SUITE_NAMES:
            secondary = fold_summary["models"].get(model_name)
            if secondary is None:
                continue
            for cohort_name, diagnostic in secondary["cohort_diagnostics"].items():
                lines.append(
                    f"| {fold_summary['fold_name']} | {model_name} | {cohort_name} | "
                    f"{diagnostic.coverage} | {diagnostic.full_mixture.mae} |"
                )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in modeled["limitations"])
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class Phase4ExperimentOutput:
    manifest_path: Path
    report_json_path: Path | None
    report_markdown_path: Path | None
    dataset_version: str
    mode: str
    selected_model: str | None


def run_phase4_validation_experiment(
    workspace: Path,
    *,
    league_fixture: Path,
    mode: str,
    now: datetime | None = None,
) -> Phase4ExperimentOutput:
    """Run the Phase 4 validation-closure experiment against cached inputs.

    ``mode="development"`` freezes (or refreshes) the manifest and runs development folds only
    -- no report is written, since a selection decision requires locked-retrospective evidence.
    ``mode="locked_retrospective"`` refuses to proceed on a missing or mismatched manifest, then
    additionally runs the 2025-26 locked-retrospective folds and writes the complete JSON and
    Markdown reports.
    """
    if mode not in ("development", "locked_retrospective"):
        raise Phase4ValidationError(f"Unknown Phase 4 experiment mode: {mode!r}")
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise Phase4ValidationError("Experiment timestamp must be timezone-aware")
    raw_dir = workspace / "raw"
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    scoring_policy = scoring_policy_from_league_fixture(league_fixture)
    inputs = load_historical_experiment_inputs(raw_dir, retrieved_at=generated_at)
    injuries = acquire_injury_archive(
        inputs.games,
        inputs.provider_players,
        workspace / "injuries",
        retrieved_at=generated_at,
        historical_player_ids_by_date_team=_historical_player_ids_by_date_team(inputs),
    )
    dataset = _build_dataset(inputs, injuries, scoring_policy, generated_at)
    backtest_config = BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    cohort_config = CohortConfig()
    component_gate_config = ComponentGateConfig()
    raw_suite = phase4_raw_suite()
    secondary_suite = phase4_secondary_calibrated_suite()
    comparison_suite = (*raw_suite, *secondary_suite)
    manifest = phase4_frozen_manifest(
        dataset=dataset,
        scoring_policy=scoring_policy,
        cohort_config=cohort_config,
        backtest_config=backtest_config,
        component_gate=component_gate_config,
        raw_suite=raw_suite,
        secondary_suite=secondary_suite,
    )
    manifest_path = reports_dir / "phase4-frozen-manifest.json"

    folds = regular_season_folds()
    development_folds = tuple(fold for fold in folds if not fold.holdout)
    development_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=comparison_suite,
        folds=development_folds,
        config=backtest_config,
        reference_model=PHASE4_DIRECT_BASELINE,
    )

    if mode == "development":
        freeze_phase4_manifest(manifest_path, manifest)
        return Phase4ExperimentOutput(
            manifest_path=manifest_path,
            report_json_path=None,
            report_markdown_path=None,
            dataset_version=dataset.dataset_version,
            mode=mode,
            selected_model=None,
        )

    assert_phase4_manifest_frozen(manifest_path, manifest)
    locked_retrospective_folds = tuple(fold for fold in folds if fold.holdout)
    locked_retrospective_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=comparison_suite,
        folds=locked_retrospective_folds,
        config=backtest_config,
        reference_model=PHASE4_DIRECT_BASELINE,
    )
    report = phase4_report(
        generated_at=generated_at,
        manifest=manifest,
        manifest_path=manifest_path,
        dataset=dataset,
        scoring_policy=scoring_policy,
        backtest_config=backtest_config,
        component_gate_config=component_gate_config,
        development_results=development_results,
        locked_retrospective_results=locked_retrospective_results,
    )
    report_json_path = reports_dir / "phase4-validation-closure-report.json"
    report_markdown_path = reports_dir / "phase4-validation-closure-report.md"
    _write_json(report_json_path, report)
    report_markdown_path.write_text(phase4_markdown_report(report))
    selection: Phase4SelectionDecision = report["modeled"]["selection"]
    return Phase4ExperimentOutput(
        manifest_path=manifest_path,
        report_json_path=report_json_path,
        report_markdown_path=report_markdown_path,
        dataset_version=dataset.dataset_version,
        mode=mode,
        selected_model=selection.selected_model,
    )
