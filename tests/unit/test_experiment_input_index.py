"""Prevent missing or conflicting team-weeks from inflating input coverage."""

import json
from pathlib import Path

import pytest
from input_index_support import sample_index
from pydantic import ValidationError

from sleeper_manager.backtesting.experiments.input_index import (
    ExperimentInputIndexError,
    build_input_inventory,
    file_reference,
    main,
    write_input_inventory,
)
from sleeper_manager.backtesting.experiments.input_index_models import (
    ExperimentInputIndex,
    FailedAttempt,
    FileReference,
    InputSelection,
    SourceBinding,
    WeekBinding,
)


@pytest.mark.parametrize("incomplete,status", [(False, "assembled"), (True, "incomplete")])
def test_full_denominator_retains_missing_and_failed_work(
    tmp_path: Path, incomplete: bool, status: str
) -> None:
    index = sample_index(tmp_path, incomplete=incomplete)
    report = build_input_inventory(index, base=tmp_path)
    assert report["expected_team_weeks"] == 2
    assert [r["status"] for r in report["team_weeks"]] == [status, "unprocessed"]
    assert report["leagues"][0]["strict_complete_team_weeks"] == 0
    assert report["team_weeks"][0]["coverage"]["best_known_eligibility"] == 1
    assert report["replay_readiness"] == "not_evaluated"


def test_identical_selection_counts_once_but_conflicting_retry_is_rejected(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    selection = index.selections[0]
    duplicate = index.model_copy(update={"selections": (selection, selection)})
    assert build_input_inventory(duplicate, base=tmp_path)["leagues"][0]["status_counts"] == {
        "assembled": 1,
        "unprocessed": 1,
    }
    retry = InputSelection(
        key=selection.key,
        failure=FailedAttempt(
            reason="source_unavailable", detail="Fixture timeout", evidence=index.protocol
        ),
    )
    with pytest.raises(ExperimentInputIndexError, match="explicit choice"):
        build_input_inventory(
            index.model_copy(update={"selections": (selection, retry)}), base=tmp_path
        )


def test_source_failure_is_counted_without_a_bundle(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    key = index.selections[0].key.model_copy(update={"roster_id": 2})
    failure = InputSelection(
        key=key,
        failure=FailedAttempt(
            reason="source_unavailable", detail="Fixture timeout", evidence=index.protocol
        ),
    )
    report = build_input_inventory(
        index.model_copy(update={"selections": (*index.selections, failure)}), base=tmp_path
    )
    assert report["team_weeks"][1]["status"] == "failed_before_assembly"
    assert report["expected_team_weeks"] == 2
    assert report["team_weeks"][1]["failure"]["reason"] == "source_unavailable"


@pytest.mark.parametrize(
    "field", ["scoring_policy_fingerprint", "league_configuration_fingerprint"]
)
def test_incompatible_league_contract_is_rejected(tmp_path: Path, field: str) -> None:
    index = sample_index(tmp_path)
    league = index.leagues[0].model_copy(update={field: "f" * 64})
    with pytest.raises(ExperimentInputIndexError, match=field):
        build_input_inventory(index.model_copy(update={"leagues": (league,)}), base=tmp_path)


@pytest.mark.parametrize(
    "field", ["builder_version", "projection_config_version", "eligibility_policy_version"]
)
def test_incompatible_version_is_rejected(tmp_path: Path, field: str) -> None:
    index = sample_index(tmp_path)
    with pytest.raises(ExperimentInputIndexError, match=field):
        build_input_inventory(index.model_copy(update={field: "other-version"}), base=tmp_path)


def test_unknown_calendar_can_be_inventoried_but_cannot_admit_a_bundle(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    league = index.leagues[0].model_copy(update={"weeks": (WeekBinding(week=1),)})
    unknown = index.model_copy(update={"leagues": (league,)})
    with pytest.raises(ExperimentInputIndexError, match="week_boundaries"):
        build_input_inventory(unknown, base=tmp_path)
    report = build_input_inventory(unknown.model_copy(update={"selections": ()}), base=tmp_path)
    assert report["leagues"][0]["unbound_calendar_weeks"] == [1]
    assert report["expected_team_weeks"] == 2


def test_shared_source_mismatch_is_rejected(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    league = index.leagues[0].model_copy(
        update={
            "shared_sources": (
                SourceBinding(name="fixture", content_hash="f" * 64, version="raw-v1"),
            )
        }
    )
    with pytest.raises(ExperimentInputIndexError, match="shared source"):
        build_input_inventory(index.model_copy(update={"leagues": (league,)}), base=tmp_path)


@pytest.mark.parametrize("roster,message", [(2, "Selected key"), (3, "outside")])
def test_selected_key_must_match_artifact_and_declared_sample(
    tmp_path: Path, roster: int, message: str
) -> None:
    index = sample_index(tmp_path)
    selection = index.selections[0]
    selection = selection.model_copy(
        update={"key": selection.key.model_copy(update={"roster_id": roster})}
    )
    with pytest.raises(ExperimentInputIndexError, match=message):
        build_input_inventory(index.model_copy(update={"selections": (selection,)}), base=tmp_path)


@pytest.mark.parametrize("target", ["manifest", "team_week"])
def test_corrupt_references_fail_even_when_files_parse(tmp_path: Path, target: str) -> None:
    index = sample_index(tmp_path)
    bundle = index.selections[0].bundle
    assert bundle is not None
    path = Path(getattr(bundle, target).path)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ExperimentInputIndexError, match="hash mismatch"):
        build_input_inventory(index, base=tmp_path)


def test_manifest_claim_is_recomputed_after_reference_hash_verification(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    selection = index.selections[0]
    assert selection.bundle is not None
    path = Path(selection.bundle.manifest.path)
    payload = json.loads(path.read_bytes())
    payload["builder_version"] = "tampered"
    path.write_text(json.dumps(payload))
    bundle = selection.bundle.model_copy(update={"manifest": file_reference(path)})
    index = index.model_copy(
        update={"selections": (selection.model_copy(update={"bundle": bundle}),)}
    )
    with pytest.raises(ExperimentInputIndexError, match="content identity"):
        build_input_inventory(index, base=tmp_path)


def test_copied_index_resolves_relative_references_and_reproduces_bytes(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    index = index.model_copy(
        update={"protocol": FileReference(path="protocol.txt", sha256=index.protocol.sha256)}
    )
    spec = tmp_path / "selected.json"
    spec.write_text(index.model_dump_json())
    first = write_input_inventory(spec, tmp_path / "reports")
    original = (first / "inventory.json").read_bytes()
    second = write_input_inventory(first / "index.json", tmp_path / "reports")
    assert first == second
    assert original == (second / "inventory.json").read_bytes()
    (first / "inventory.json").write_text("changed")
    with pytest.raises(ExperimentInputIndexError, match="immutable"):
        write_input_inventory(spec, tmp_path / "reports")


def test_schema_rejects_duplicate_denominators_and_untyped_keys(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    for change in (
        {"roster_ids": [1, 1]},
        {"roster_ids": [True]},
        {"weeks": [{"week": 1}, {"week": 1}]},
    ):
        payload = index.model_dump(mode="json")
        payload["leagues"][0].update(change)
        with pytest.raises(ValidationError):
            ExperimentInputIndex.model_validate(payload)


def test_cli_writes_inventory_and_reports_missing_sources(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    spec = tmp_path / "index.json"
    spec.write_text(index.model_dump_json())
    assert main([str(spec), "--output-root", str(tmp_path / "reports")]) == 0
    Path(index.protocol.path).unlink()
    with pytest.raises(SystemExit) as caught:
        main([str(spec), "--output-root", str(tmp_path / "reports")])
    assert caught.value.code == 1


def test_separate_league_manifests_form_one_inventory(tmp_path: Path) -> None:
    from dataclasses import replace

    from historical_team_week_artifact_support import _manifest, _team_week

    from sleeper_manager.backtesting.experiments.input_index_models import (
        BundleReference,
        TeamWeekKey,
    )
    from sleeper_manager.backtesting.replay.inputs import write_replay_input_bundle

    index = sample_index(tmp_path)
    manifest = replace(
        _manifest(),
        league_id="league-2",
        scoring_policy_fingerprint="a" * 64,
        league_configuration_fingerprint="b" * 64,
        week_boundaries_fingerprint="c" * 64,
    )
    team_week = replace(_team_week(manifest.manifest_id), league_id="league-2", player_games=())
    root = write_replay_input_bundle(tmp_path / "inputs", manifest, (team_week,))
    league = index.leagues[0].model_copy(update={"league_id": "league-2", "role": "stress"})
    selection = InputSelection(
        key=TeamWeekKey(league_id="league-2", season="2026", week=1, roster_id=1),
        bundle=BundleReference(
            manifest=file_reference(root / "manifest.json"),
            team_week=file_reference(root / "team-weeks/league-2/week-01/roster-1.json"),
        ),
    )
    report = build_input_inventory(
        index.model_copy(
            update={
                "leagues": (*index.leagues, league),
                "selections": (*index.selections, selection),
            }
        ),
        base=tmp_path,
    )
    assert report["expected_team_weeks"] == 4
    assert report["leagues"][0]["status_counts"] == {"assembled": 1, "unprocessed": 1}
    assert report["leagues"][1]["status_counts"] == {"incomplete": 1, "unprocessed": 1}
    assert report["team_weeks"][0]["manifest_id"] != report["team_weeks"][2]["manifest_id"]


def test_projection_versions_are_checked_against_manifest(tmp_path: Path) -> None:
    index = sample_index(tmp_path)
    selection = index.selections[0]
    assert selection.bundle is not None
    path = Path(selection.bundle.team_week.path)
    payload = json.loads(path.read_bytes())
    payload["player_games"][0]["projection"]["model_version"] = "different-model"
    path.write_text(json.dumps(payload))
    bundle = selection.bundle.model_copy(update={"team_week": file_reference(path)})
    index = index.model_copy(
        update={"selections": (selection.model_copy(update={"bundle": bundle}),)}
    )
    with pytest.raises(ExperimentInputIndexError, match="Projection disagrees"):
        build_input_inventory(index, base=tmp_path)
