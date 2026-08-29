"""Shared Sleeper archive fixtures for historical team-week tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

RETRIEVED_AT = datetime(2026, 8, 26, tzinfo=UTC)
LEAGUE_ID = "1263724135977070592"


def _write_archive(
    workspace: Path,
    *,
    include_bench: bool = False,
    season: str = "2026",
) -> None:
    """Write the minimum immutable Sleeper cache needed for the selected fixture week."""

    root = workspace / "sleeper" / LEAGUE_ID
    week_root = root / "weeks" / "16"
    week_root.mkdir(parents=True)
    _write_json(
        root / "league.json",
        {
            "league_id": LEAGUE_ID,
            "sport": "nba",
            "season": season,
            "season_type": "regular",
            "status": "complete",
            "total_rosters": 16,
            "roster_positions": ["PG", "BN"],
            "scoring_settings": {"pts": 1},
        },
    )
    player_ids = ["sleeper-1", *(["sleeper-2"] if include_bench else [])]
    _write_json(root / "rosters.json", [{"roster_id": 4, "players": player_ids}])
    _write_json(
        root / "players.json",
        {
            "sleeper-1": {
                "full_name": "Fixture Guard",
                "team": "CHI",
                "espn_id": "provider-1",
                "fantasy_positions": ["PG"],
            },
            **(
                {
                    "sleeper-2": {
                        "full_name": "Fixture Bench",
                        "team": "CHI",
                        "espn_id": "provider-2",
                        "fantasy_positions": ["SG"],
                    }
                }
                if include_bench
                else {}
            ),
            "unrelated-malformed-player": {},
        },
    )
    _write_json(
        week_root / "matchups.json",
        [{"roster_id": 4, "players": player_ids, "starters": ["sleeper-1"]}],
    )
    _write_json(week_root / "transactions.json", [])


def _write_unrelated_week(workspace: Path) -> None:
    """Write a stale week whose contents must not participate in the selected input."""

    week_root = workspace / "sleeper" / LEAGUE_ID / "weeks" / "15"
    week_root.mkdir(parents=True)
    _write_json(
        week_root / "matchups.json",
        [{"roster_id": 4, "players": ["unrelated-player"], "starters": ["unrelated-player"]}],
    )
    _write_json(
        week_root / "transactions.json",
        [
            {
                "transaction_id": "unrelated",
                "type": "free_agent",
                "status": "complete",
                "status_updated": 0,
                "adds": {"unrelated-player": 4},
                "drops": {},
            }
        ],
    )


def _write_json(path: Path, payload: object) -> None:
    """Write one cache payload with stable JSON encoding for the fixture archive."""

    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
