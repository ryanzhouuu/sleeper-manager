"""Exercise supplemental outcomes through full bundle assembly and fingerprints."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from inactive_evidence_support import inactive_fixture
from team_week_source_support import LEAGUE_ID, RETRIEVED_AT, _write_archive

from sleeper_manager.backtesting.replay.team_week_bundle import (
    HistoricalTeamWeekBundleError,
    HistoricalTeamWeekBundleRequest,
    bootstrap_historical_team_week_bundle,
)


def test_supplement_recovers_scoring_without_changing_membership_or_history(tmp_path: Path) -> None:
    _write_archive(tmp_path)
    inputs, ledger = inactive_fixture(tmp_path)
    earlier = replace(
        inputs.games[0], provider_id="earlier", start_time=datetime(2026, 1, 30, 18, tzinfo=UTC)
    )
    inputs = replace(inputs, games=(earlier, *inputs.games))
    earlier_pdf = b"%PDF-1.4 synthetic earlier game report"
    (tmp_path / "earlier.pdf").write_bytes(earlier_pdf)
    payload = json.loads(ledger.read_text())
    payload["records"].append(
        {
            **payload["records"][0],
            "game_id": "earlier",
            "game_start": earlier.start_time.isoformat(),
            "report_url": "https://statsdmz.nba.com/pdfs/20260130/20260130_BOSCHI.pdf",
            "pdf_path": "earlier.pdf",
            "pdf_sha256": hashlib.sha256(earlier_pdf).hexdigest(),
        }
    )
    ledger.write_text(json.dumps(payload))
    request = HistoricalTeamWeekBundleRequest(LEAGUE_ID, 4, 16, date(2026, 2, 2))
    with pytest.raises(HistoricalTeamWeekBundleError, match="no joined"):
        bootstrap_historical_team_week_bundle(
            tmp_path, request, nba_inputs=inputs, now=RETRIEVED_AT
        )
    recovered = bootstrap_historical_team_week_bundle(
        tmp_path, request, nba_inputs=inputs, inactive_evidence_path=ledger, now=RETRIEVED_AT
    )
    coverage = recovered.team_week.coverage
    assert (
        coverage.expected_player_games
        == coverage.joined_player_games
        == coverage.scored_player_games
        == 3
    )
    assert coverage.projected_player_games == 3
    assert coverage.inferred_team_membership == 1
    assert not coverage.complete
    assert not recovered.team_week.exclusions
    inactive = next(row for row in recovered.team_week.player_games if row.game_id == "missing")
    assert inactive.actual_score == 0
    assert inactive.projection is not None
    assert recovered.manifest.builder_version == "historical-team-week-bundle-v6"
    original_manifest = recovered.manifest_path.read_bytes()
    payload = json.loads(ledger.read_text())
    payload["records"][0]["verification"] += " additional review"
    ledger.write_text(json.dumps(payload))
    reviewed_again = bootstrap_historical_team_week_bundle(
        tmp_path, request, nba_inputs=inputs, inactive_evidence_path=ledger, now=RETRIEVED_AT
    )
    assert reviewed_again.manifest.manifest_id != recovered.manifest.manifest_id
    assert recovered.manifest_path.read_bytes() == original_manifest
    assert reviewed_again.team_week.coverage == coverage
    ordinary = bootstrap_historical_team_week_bundle(
        tmp_path, request, nba_inputs=replace(inputs, games=inputs.games[:-1]), now=RETRIEVED_AT
    )
    recovered_target = next(
        row.projection for row in recovered.team_week.player_games if row.game_id == "target"
    )
    ordinary_target = next(
        row.projection for row in ordinary.team_week.player_games if row.game_id == "target"
    )
    assert recovered_target is not None and ordinary_target is not None
    assert recovered_target.distribution == ordinary_target.distribution
    assert recovered_target.input_version == ordinary_target.input_version


def test_final_inactive_cannot_resolve_an_unknown_membership_endpoint(tmp_path: Path) -> None:
    _write_archive(tmp_path)
    inputs, ledger = inactive_fixture(tmp_path)
    inputs = replace(
        inputs,
        player_box_scores=tuple(box for box in inputs.player_box_scores if box.game_id != "target"),
    )
    with pytest.raises(HistoricalTeamWeekBundleError, match="no joined"):
        bootstrap_historical_team_week_bundle(
            tmp_path,
            HistoricalTeamWeekBundleRequest(LEAGUE_ID, 4, 16, date(2026, 2, 2)),
            nba_inputs=inputs,
            inactive_evidence_path=ledger,
            now=RETRIEVED_AT,
        )
