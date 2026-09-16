"""Validate reviewed official game-roster censuses and their source bindings."""

import hashlib
import json
from pathlib import Path

import pytest

from sleeper_manager.backtesting.replay.final_roster_evidence import (
    load_final_roster_evidence,
)
from tests.sleeper_manager.backtesting.replay.inactive_evidence_support import inactive_fixture


def _ledger(tmp_path: Path) -> tuple[object, Path]:
    """Create one scoped complete-roster review backed by a synthetic final PDF."""

    inputs, _ = inactive_fixture(tmp_path)
    game = next(game for game in inputs.games if game.provider_id == "missing")
    raw = b"%PDF-1.4 synthetic complete game roster"
    pdf = tmp_path / "roster.pdf"
    pdf.write_bytes(raw)
    ledger = tmp_path / "rosters.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "reviewed-final-game-rosters-v1",
                "reports": [
                    {
                        "game_id": game.provider_id,
                        "game_start": game.start_time.isoformat(),
                        "home_team_id": game.home_team_id,
                        "away_team_id": game.away_team_id,
                        "pdf_path": pdf.name,
                        "pdf_sha256": hashlib.sha256(raw).hexdigest(),
                        "report_url": "https://statsdmz.nba.com/pdfs/20260202/20260202_BOSCHI.pdf",
                        "page": 1,
                        "retrieved_at": "2026-08-27T00:00:00Z",
                        "reviewed_player_ids": ["provider-1"],
                        "player_teams": [["provider-1", "CHI"]],
                        "verification": "Synthetic complete game-roster review",
                    }
                ],
            }
        )
    )
    return inputs, ledger


def test_reviewed_final_report_supplies_a_scoped_complete_roster(tmp_path: Path) -> None:
    inputs, ledger = _ledger(tmp_path)
    evidence = load_final_roster_evidence(ledger, inputs)

    assert len(evidence.censuses) == 1
    census = evidence.censuses[0]
    assert census.game_id == "missing"
    assert census.reviewed_player_ids == ("provider-1",)
    assert census.player_teams == (("provider-1", "CHI"),)
    assert len(evidence.source_fingerprints) == 2


@pytest.mark.parametrize("mode", ["hash", "game", "team", "player"])
def test_invalid_roster_review_is_rejected(tmp_path: Path, mode: str) -> None:
    inputs, ledger = _ledger(tmp_path)
    payload = json.loads(ledger.read_text())
    report = payload["reports"][0]
    if mode == "hash":
        report["pdf_sha256"] = "0" * 64
    elif mode == "game":
        report["game_id"] = "absent"
    elif mode == "team":
        report["player_teams"][0][1] = "other"
    else:
        report["reviewed_player_ids"] = ["absent"]
        report["player_teams"][0][0] = "absent"
    ledger.write_text(json.dumps(payload))

    with pytest.raises((ValueError, RuntimeError)):
        load_final_roster_evidence(ledger, inputs)
