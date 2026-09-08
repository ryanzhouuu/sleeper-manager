"""Synthetic reviewed evidence for an outcome missing between agreeing team records."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from team_week_projection_support import _nba_inputs

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs


def inactive_fixture(tmp_path: Path) -> tuple[HistoricalExperimentInputs, Path]:
    inputs = _nba_inputs()
    missing = replace(
        inputs.games[-1], provider_id="missing", start_time=datetime(2026, 2, 2, 18, tzinfo=UTC)
    )
    pdf = b"%PDF-1.4 synthetic reviewed final report fixture"
    (tmp_path / "final.pdf").write_bytes(pdf)
    path = tmp_path / "inactive.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "reviewed-final-inactive-v1",
                "records": [
                    {
                        "player_id": "provider-1",
                        "player_name": "Fixture Guard",
                        "game_id": "missing",
                        "team_id": "CHI",
                        "game_start": missing.start_time.isoformat(),
                        "status": "confirmed_final_inactive",
                        "pdf_path": "final.pdf",
                        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                        "report_url": "https://statsdmz.nba.com/pdfs/20260202/20260202_BOSCHI.pdf",
                        "page": 1,
                        "retrieved_at": "2026-08-27T00:00:00Z",
                        "verification": "Synthetic final inactive review fixture",
                    }
                ],
            }
        )
    )
    return replace(inputs, games=(*inputs.games, missing)), path
