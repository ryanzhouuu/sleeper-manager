"""Rendering and persistence helpers for replay reports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.replay.validation import ReplayMetricSummary


def report_payload(
    *,
    league_id: str,
    season: str,
    oracle_label: str,
    summary: ReplayMetricSummary,
    exclusions: tuple[tuple[str, int], ...] = (),
) -> dict[str, Any]:
    return {
        "league_id": league_id,
        "season": season,
        "oracle_label": oracle_label,
        "primary": asdict(summary),
        "exclusions": [{"reason": reason, "count": count} for reason, count in exclusions],
    }


def markdown_report(payloads: tuple[dict[str, Any], ...]) -> str:
    lines = [
        "# Lock-In validation report",
        "",
        "Team-week score, constrained-oracle regret, and score capture are primary outcomes.",
        "",
    ]
    for payload in payloads:
        primary = payload["primary"]
        lines.extend(
            (
                f"## League {payload['league_id']} ({payload['season']})",
                "",
                f"- Oracle label: `{payload['oracle_label']}`",
                f"- Complete team-weeks: {primary['team_week_count']}",
                f"- Mean model-policy regret: {primary['mean_regret']}",
                f"- Mean score capture: {primary['mean_score_capture']}",
                f"- Aggregate score capture: {primary['aggregate_score_capture']}",
                f"- Excluded team-weeks: {primary['excluded_team_weeks']}",
                "",
            )
        )
    return "\n".join(lines)


def write_report(path: Path, payload: dict[str, Any], *, markdown_path: Path | None = None) -> None:
    _atomic_write(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    if markdown_path is not None:
        _atomic_write(markdown_path, markdown_report((payload,)))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


__all__ = ("markdown_report", "report_payload", "write_report")
