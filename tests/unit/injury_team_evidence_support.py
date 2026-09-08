"""Synthetic injury caches tied to the projection fixture's final game."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from team_week_projection_support import _nba_inputs

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs
from sleeper_manager.domain.nba import Team


def nba_fixture() -> HistoricalExperimentInputs:
    inputs = _nba_inputs()
    source = inputs.games[0].source
    return replace(
        inputs,
        games=tuple(game for game in inputs.games if game.provider_id != "same-day"),
        teams=(
            Team("CHI", "CHI", "Chicago Bulls", None, source),
            Team("BOS", "BOS", "Boston Celtics", None, source),
        ),
    )


def report_cache(
    tmp_path: Path,
    name: str,
    *,
    hour: int = 19,
    included: bool = True,
    pending: bool = False,
    submitted: bool = True,
) -> Path:
    pdf = f"%PDF-1.4 synthetic {name}".encode()
    digest = hashlib.sha256(pdf).hexdigest()
    published = datetime(2026, 2, 2, hour, tzinfo=UTC).isoformat()
    context = {
        "game_date": "2026-02-02",
        "game_time": "15:00:00",
        "matchup": "BOS@CHI",
        "team_abbreviation": "chi",
        "team_name": "Chicago Bulls",
    }
    entry = {
        **context,
        "player_name": "Guard, Fixture",
        "status": "out",
        "reason": "Trade Pending" if pending else "Injury/Illness - Knee; Soreness",
    }
    payload = {
        "cache_schema_version": "2",
        "pdf_sha256": digest,
        "snapshot": {
            "published_at": published,
            "entries": [entry] if included else [],
            "team_statuses": [
                {**context, "status": "submitted" if submitted else "not_yet_submitted"}
            ],
            "source": {
                "provider": "nba_official_injury_report",
                "provider_id": "fixture",
                "retrieved_at": "2026-09-08T00:00:00Z",
                "source_updated_at": published,
                "schema_version": "2",
                "content_hash": digest,
            },
        },
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    path.with_suffix(".pdf").write_bytes(pdf)
    return path


def report_ledger(tmp_path: Path, paths: list[Path]) -> Path:
    path = tmp_path / "reports.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "injury-team-proxies-v1",
                "reports": [
                    {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in paths
                ],
            }
        )
    )
    return path
