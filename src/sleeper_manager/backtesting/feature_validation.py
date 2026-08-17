from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
from sleeper_manager.backtesting.experiment_io import (
    _json_value,
    _write_json,
)
from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestModel,
    ProjectionModel,
)
from sleeper_manager.backtesting.validation_folds import (
    regular_season_folds,
    run_validation_folds,
)
from sleeper_manager.backtesting.validation_gates import (
    evaluate_development_candidate,
    evaluate_promotion,
)
from sleeper_manager.backtesting.validation_metrics import (
    block_bootstrap_mae_delta,
    segment_comparisons,
)
from sleeper_manager.backtesting.validation_models import (
    DevelopmentDecision,
    FoldResult,
    PromotionDecision,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    build_historical_feature_dataset,
)
from sleeper_manager.integrations.nba.historical_feature_models import (
    FEATURE_SCHEMA_VERSION,
    HistoricalFeatureDataset,
)
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.official_injury_mapping import InjuryMappingDiagnostic
from sleeper_manager.integrations.nba.official_injury_models import EASTERN_TIME
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
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


def _assert_frozen_manifest(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        actual = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ModelFeatureValidationError("Frozen development manifest is unreadable") from error
    if actual != _json_value(expected):
        raise ModelFeatureValidationError("Frozen development manifest drifted before holdout")
