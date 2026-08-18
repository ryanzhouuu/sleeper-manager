from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.replay.league_archive import (
    HistoricalLeagueArchive,
    acquire_sleeper_archive,
    atomic_write_json,
    parse_historical_league_archive,
    resolve_predecessor_chain,
)
from sleeper_manager.backtesting.replay.report import markdown_report
from sleeper_manager.integrations.sleeper.client import SleeperClient


class LockInExperimentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LockInValidationOutput:
    status: str
    report_json_path: Path
    report_markdown_path: Path
    league_ids: tuple[str, ...]


def run_lock_in_policy_validation(
    workspace: Path,
    *,
    current_league_id: str,
    historical_league_id: str,
    stress_league_id: str,
    refresh_sleeper: bool = False,
) -> LockInValidationOutput:
    if refresh_sleeper:
        asyncio.run(
            _refresh_sleeper_archives(
                workspace, (current_league_id, historical_league_id, stress_league_id)
            )
        )
    requested_ids = (current_league_id, historical_league_id, stress_league_id)
    archives = {
        league_id: _load_cached_archive(workspace, league_id) for league_id in requested_ids
    }
    current = archives[current_league_id]
    for archive in archives.values():
        _validate_current_configuration(current, archive)
    reports = tuple(
        _empty_league_report(archive, primary=archive.league_id == historical_league_id)
        for archive in archives.values()
    )
    report = {
        "status": "blocked_pending_nba_replay_inputs",
        "generated_at": datetime.now(UTC).isoformat(),
        "release_decision": "do_not_promote",
        "reason": "Sleeper archives loaded, but NBA replay inputs are not present in this cache.",
        "leagues": reports,
    }
    report_dir = workspace / "reports"
    report_json = report_dir / "latest-lock-in-validation-report.json"
    report_markdown = report_dir / "latest-lock-in-validation-report.md"
    atomic_write_json(report_json, report)
    report_markdown.parent.mkdir(parents=True, exist_ok=True)
    report_markdown.write_text(markdown_report(tuple(reports)), encoding="utf-8")
    return LockInValidationOutput(
        status=str(report["status"]),
        report_json_path=report_json,
        report_markdown_path=report_markdown,
        league_ids=requested_ids,
    )


async def _refresh_sleeper_archives(workspace: Path, league_ids: tuple[str, ...]) -> None:
    root = workspace / "sleeper"
    weeks = range(1, 27)
    async with SleeperClient() as reader:
        pending = list(dict.fromkeys(league_ids))
        seen: set[str] = set()
        while pending:
            league_id = pending.pop(0)
            if league_id in seen:
                continue
            seen.add(league_id)
            payload = await reader.league(league_id)
            await acquire_sleeper_archive(
                reader,
                league_id=league_id,
                root=root,
                weeks=weeks,
                retrieved_at=datetime.now(UTC),
            )
            previous = payload.get("previous_league_id")
            if previous not in (None, ""):
                pending.append(str(previous))


def _load_cached_archive(workspace: Path, requested_league_id: str) -> HistoricalLeagueArchive:
    root = workspace / "sleeper"
    payloads: dict[str, dict[str, Any]] = {}
    initial = _read_json(root / requested_league_id / "league.json")
    payloads[requested_league_id] = initial
    previous = initial.get("previous_league_id")
    while previous:
        previous_id = str(previous)
        if previous_id in payloads:
            raise LockInExperimentError("Cached predecessor chain contains a cycle")
        payloads[previous_id] = _read_json(root / previous_id / "league.json")
        previous = payloads[previous_id].get("previous_league_id")
    chain = resolve_predecessor_chain(requested_league_id, payloads)
    resolved_id = next(
        (league_id for league_id in chain if _has_archive_events(root / league_id)),
        requested_league_id,
    )
    archive_root = root / resolved_id
    league_payload = payloads[resolved_id]
    retrieved_at = _retrieved_at(archive_root)
    weeks: dict[int, list[dict[str, Any]]] = {}
    transactions: list[dict[str, Any]] = []
    for week_dir in (
        sorted((archive_root / "weeks").glob("*")) if (archive_root / "weeks").is_dir() else ()
    ):
        try:
            week = int(week_dir.name)
        except ValueError:
            continue
        matchup_path = week_dir / "matchups.json"
        transaction_path = week_dir / "transactions.json"
        if matchup_path.is_file():
            values = _read_json(matchup_path)
            if isinstance(values, list):
                weeks[week] = [value for value in values if isinstance(value, dict)]
        if transaction_path.is_file():
            values = _read_json(transaction_path)
            if isinstance(values, list):
                transactions.extend(value for value in values if isinstance(value, dict))
    players_payload = _read_json(archive_root / "players.json", required=False)
    if not isinstance(players_payload, dict):
        players_payload = {}
    return parse_historical_league_archive(
        league_payload,
        rosters=_read_list(archive_root / "rosters.json"),
        matchup_weeks=weeks,
        transactions=transactions,
        player_catalog=players_payload,
        retrieved_at=retrieved_at,
        resolved_from_league_id=requested_league_id if resolved_id != requested_league_id else None,
    )


def _empty_league_report(archive: HistoricalLeagueArchive, *, primary: bool) -> dict[str, Any]:
    return {
        "league_id": archive.league_id,
        "resolved_from_league_id": archive.resolved_from_league_id,
        "season": archive.season,
        "role": "primary_historical" if primary else "current_or_stress",
        "oracle_label": "best_known_constraints_oracle",
        "primary": {
            "team_week_count": 0,
            "mean_regret": None,
            "mean_score_capture": None,
            "excluded_team_weeks": 0,
        },
        "coverage": {
            "rosters": len(archive.final_rosters),
            "matchup_records": len(archive.matchup_weeks),
            "transactions": len(archive.transactions),
            "eligibility_records": len(archive.player_eligibility),
        },
    }


def _validate_current_configuration(
    current: HistoricalLeagueArchive, archive: HistoricalLeagueArchive
) -> None:
    if archive.scoring_policy.fingerprint != current.scoring_policy.fingerprint:
        raise LockInExperimentError(
            f"Scoring policy mismatch for {archive.league_id}; refusing replay validation"
        )
    current_starters = tuple(slot for slot in current.roster_slots if slot not in {"BN", "IR"})
    archive_starters = tuple(slot for slot in archive.roster_slots if slot not in {"BN", "IR"})
    if len(current_starters) != len(archive_starters):
        raise LockInExperimentError(
            f"Starter-slot count mismatch for {archive.league_id}; refusing replay validation"
        )


def _read_json(path: Path, *, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise LockInExperimentError(f"Missing cached Sleeper artifact: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LockInExperimentError(f"Invalid cached Sleeper artifact: {path}") from error


def _read_list(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, list) or not all(isinstance(value, dict) for value in payload):
        raise LockInExperimentError(f"Cached Sleeper artifact is not an object list: {path}")
    return payload


def _has_archive_events(path: Path) -> bool:
    weeks = path / "weeks"
    if not weeks.is_dir():
        return False
    for matchup_path in weeks.glob("*/matchups.json"):
        payload = _read_json(matchup_path)
        if isinstance(payload, list) and any(isinstance(value, dict) for value in payload):
            return True
    return False


def _retrieved_at(path: Path) -> datetime:
    manifest = _read_json(path / "manifest.json", required=False)
    value = manifest.get("retrieved_at") if isinstance(manifest, dict) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


__all__ = (
    "LockInExperimentError",
    "LockInValidationOutput",
    "run_lock_in_policy_validation",
)
