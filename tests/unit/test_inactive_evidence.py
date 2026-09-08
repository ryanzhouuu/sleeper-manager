"""Reject unverified or contradictory supplemental scoring evidence."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from inactive_evidence_support import inactive_fixture

from sleeper_manager.backtesting.replay.inactive_evidence import load_inactive_evidence
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.scoring import BoxScoreLine


def test_verified_final_inactive_recovers_only_explicit_outcome(tmp_path: Path) -> None:
    inputs, path = inactive_fixture(tmp_path)
    evidence = load_inactive_evidence(path, inputs)
    assert len(evidence.box_scores) == 1
    box = evidence.box_scores[0]
    assert (box.game_id, box.player_id, box.team_id) == ("missing", "provider-1", "CHI")
    assert not box.did_play and not box.started
    assert box.line == BoxScoreLine() and box.minutes == 0
    assert len(evidence.source_fingerprints) == 2
    assert box.source.content_hash
    assert box.source.source_updated_at is None
    assert len(inputs.player_box_scores) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "out"),
        ("player_id", "absent"),
        ("player_name", "Another Person"),
        ("team_id", "other-team"),
        ("game_id", "absent"),
        ("page", 0),
        ("game_start", "2026-02-02T19:00:00Z"),
        ("game_start", "2026-02-02T18:00:00"),
        ("retrieved_at", "2026-02-01T00:00:00Z"),
        ("pdf_sha256", "0" * 64),
        ("report_url", "https://example.com/report.pdf"),
    ],
)
def test_invalid_review_record_cannot_fill_missing_evidence(
    tmp_path: Path, field: str, value: object
) -> None:
    inputs, path = inactive_fixture(tmp_path)
    payload = json.loads(path.read_text())
    payload["records"][0][field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(
        ValueError
        if field in {"status", "page", "report_url"} or value == "2026-02-02T18:00:00"
        else RuntimeError
    ):
        load_inactive_evidence(path, inputs)


@pytest.mark.parametrize(
    "mode", ["duplicate", "changed_pdf", "missing_pdf", "nonfinal", "played", "points", "team"]
)
def test_conflicting_or_unavailable_evidence_is_rejected(tmp_path: Path, mode: str) -> None:
    inputs, path = inactive_fixture(tmp_path)
    if mode == "duplicate":
        payload = json.loads(path.read_text())
        payload["records"] *= 2
        path.write_text(json.dumps(payload))
    elif mode == "changed_pdf":
        (tmp_path / "final.pdf").write_bytes(b"%PDF-altered")
    elif mode == "missing_pdf":
        (tmp_path / "final.pdf").unlink()
    elif mode == "nonfinal":
        inputs = replace(
            inputs,
            games=(*inputs.games[:-1], replace(inputs.games[-1], status=GameStatus.SCHEDULED)),
        )
    else:
        box = load_inactive_evidence(path, inputs).box_scores[0]
        box = replace(
            box,
            did_play=mode == "played",
            line=BoxScoreLine(points=1 if mode == "points" else 0),
            team_id="BOS" if mode == "team" else "CHI",
        )
        inputs = replace(inputs, player_box_scores=(*inputs.player_box_scores, box))
    with pytest.raises((RuntimeError, OSError)):
        load_inactive_evidence(path, inputs)


def test_existing_zero_outcome_retains_both_sources(tmp_path: Path) -> None:
    inputs, path = inactive_fixture(tmp_path)
    original = load_inactive_evidence(path, inputs).box_scores[0]
    original = replace(original, source=inputs.player_box_scores[0].source)
    updated = load_inactive_evidence(
        path, replace(inputs, player_box_scores=(*inputs.player_box_scores, original))
    )
    assert updated.box_scores[0].source == original.source
    assert len(updated.box_scores[0].additional_sources) == 1


def test_empty_review_does_not_invent_inactive_outcomes(tmp_path: Path) -> None:
    inputs, path = inactive_fixture(tmp_path)
    path.write_text(json.dumps({"schema_version": "reviewed-final-inactive-v1", "records": []}))
    assert load_inactive_evidence(path, inputs).box_scores == ()


def test_duplicate_existing_outcomes_cannot_hide_a_played_result(tmp_path: Path) -> None:
    inputs, path = inactive_fixture(tmp_path)
    zero = load_inactive_evidence(path, inputs).box_scores[0]
    played = replace(zero, did_play=True, minutes=20, line=BoxScoreLine(points=10))
    for duplicates in ((zero, played), (played, zero)):
        with pytest.raises(RuntimeError, match="duplicate outcomes"):
            load_inactive_evidence(
                path, replace(inputs, player_box_scores=(*inputs.player_box_scores, *duplicates))
            )
