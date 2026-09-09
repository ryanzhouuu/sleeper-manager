"""Small immutable bundles for inventory accounting and compatibility tests."""

from dataclasses import replace
from pathlib import Path

from historical_team_week_artifact_support import _manifest, _team_week

from sleeper_manager.backtesting.experiments.input_index import file_reference
from sleeper_manager.backtesting.experiments.input_index_models import (
    BundleReference,
    ExperimentInputIndex,
    InputSelection,
    LeagueSample,
    TeamWeekKey,
    WeekBinding,
)
from sleeper_manager.backtesting.replay.inputs import write_replay_input_bundle


def sample_index(root: Path, *, incomplete: bool = False) -> ExperimentInputIndex:
    root.mkdir(exist_ok=True)
    evidence = root / "protocol.txt"
    evidence.write_text("Fixture sample and protocol evidence")
    manifest = replace(
        _manifest(),
        scoring_policy_fingerprint="a" * 64,
        league_configuration_fingerprint="b" * 64,
        week_boundaries_fingerprint="c" * 64,
    )
    team_week = _team_week(manifest.manifest_id)
    if not incomplete:
        team_week = replace(
            team_week, exclusions=(), coverage=replace(team_week.coverage, missing_evidence=())
        )
    else:
        team_week = replace(team_week, player_games=())
    bundle = write_replay_input_bundle(root / "inputs", manifest, (team_week,))
    selected = InputSelection(
        key=TeamWeekKey(league_id="league-1", season="2026", week=1, roster_id=1),
        bundle=BundleReference(
            manifest=file_reference(bundle / "manifest.json"),
            team_week=file_reference(bundle / "team-weeks/league-1/week-01/roster-1.json"),
        ),
    )
    return ExperimentInputIndex(
        protocol=file_reference(evidence),
        executor_commit="d" * 40,
        variant="fixture",
        builder_version=manifest.builder_version,
        projection_config_version=manifest.projection_config_version,
        eligibility_policy_version=manifest.eligibility_policy_version,
        unresolved_bindings=("full_advisor_executor",),
        leagues=(
            LeagueSample(
                league_id="league-1",
                season="2026",
                role="primary",
                roster_ids=(1, 2),
                weeks=(WeekBinding(week=1, boundaries_fingerprint="c" * 64),),
                scoring_policy_fingerprint="a" * 64,
                league_configuration_fingerprint="b" * 64,
                evidence=(file_reference(evidence),),
            ),
        ),
        selections=(selected,),
    )
