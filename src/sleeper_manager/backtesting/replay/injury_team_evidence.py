"""Associate latest pregame team reports with completed games as labeled roster proxies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs
from sleeper_manager.backtesting.replay.inputs import SourceFingerprint, source_fingerprint
from sleeper_manager.backtesting.replay.inputs.models import PlayerTeamObservation
from sleeper_manager.backtesting.replay.roster_timeline import EASTERN_TIME
from sleeper_manager.backtesting.replay.team_week_sources import HistoricalTeamWeekBundleError
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.integrations.nba.mapping import (
    normalize_player_name,
    normalize_report_player_name,
    normalize_team,
)
from sleeper_manager.integrations.nba.official_injury_models import (
    OfficialInjuryReportSnapshot,
    ReportSubmissionStatus,
    deserialize_official_injury_report_snapshot,
)


class _ReportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ReportLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["injury-team-proxies-v1"]
    reports: tuple[_ReportSource, ...]


class _ReportCache(BaseModel):
    cache_schema_version: Literal["2"]
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class InjuryTeamEvidence:
    """Approximate team associations, with immutable source and policy fingerprints."""

    observations: tuple[PlayerTeamObservation, ...] = ()
    source_fingerprints: tuple[SourceFingerprint, ...] = ()


def load_injury_team_evidence(
    path: Path, nba_inputs: HistoricalExperimentInputs
) -> InjuryTeamEvidence:
    """Use latest supplied team snapshots by tipoff; never carry forward an omitted player.

    Tied reports must agree on the player association. Trade Pending entries are
    excluded. This is a retrospective approximation, not exact transaction history.
    """
    snapshots, fingerprints = _read_reports(path)
    abbreviations = {
        team.provider_id: normalize_team(team.abbreviation) for team in nba_inputs.teams
    }
    games_by_key: dict[tuple[date, str | None, str | None], list[ScheduledGame]] = {}
    for game in nba_inputs.games:
        if game.status is GameStatus.FINAL:
            key = (
                game.start_time.astimezone(EASTERN_TIME).date(),
                abbreviations.get(game.away_team_id),
                abbreviations.get(game.home_team_id),
            )
            games_by_key.setdefault(key, []).append(game)
    names: dict[str, set[str]] = {}
    for player in nba_inputs.provider_players:
        names.setdefault(normalize_player_name(player.full_name), set()).add(player.provider_id)
    latest: dict[
        tuple[str, str], tuple[datetime, list[OfficialInjuryReportSnapshot], ScheduledGame]
    ] = {}
    for snapshot in snapshots:
        for status in snapshot.team_statuses:
            parts = tuple(normalize_team(part) for part in status.matchup.split("@"))
            if len(parts) != 2:
                continue
            games = games_by_key.get((status.game_date, *parts), [])
            if len(games) != 1 or snapshot.published_at > games[0].start_time:
                continue
            game = games[0]
            team_id = next(
                (
                    team
                    for team in (game.home_team_id, game.away_team_id)
                    if abbreviations.get(team) == normalize_team(status.team_abbreviation)
                ),
                None,
            )
            if team_id is None:
                continue
            report_key = (game.provider_id, team_id)
            if report_key not in latest or snapshot.published_at > latest[report_key][0]:
                latest[report_key] = (snapshot.published_at, [snapshot], game)
            elif (
                snapshot.published_at == latest[report_key][0]
                and snapshot not in latest[report_key][1]
            ):
                latest[report_key][1].append(snapshot)
    observations = []
    for (_, team_id), (_, reports, game) in sorted(latest.items()):
        associations = [
            _associations(report, game, team_id, abbreviations, names) for report in reports
        ]
        for player_id in sorted(set.intersection(*associations)):
            hashes = ",".join(sorted({report.source.content_hash or "" for report in reports}))
            observations.append(
                PlayerTeamObservation(
                    player_id,
                    team_id,
                    game.start_time,
                    f"injury-team-proxies-v1:{hashes}:{game.provider_id}:{player_id}",
                    approximate=True,
                )
            )
    return InjuryTeamEvidence(tuple(observations), fingerprints)


def _associations(
    report: OfficialInjuryReportSnapshot,
    game: ScheduledGame,
    team_id: str,
    abbreviations: dict[str, str | None],
    names: dict[str, set[str]],
) -> set[str]:
    """Only explicit non-pending entries in a submitted team report support a proxy."""
    game_date = game.start_time.astimezone(EASTERN_TIME).date()
    matchup = (abbreviations[game.away_team_id], abbreviations[game.home_team_id])

    def matches(day: date, pairing: str, team: str) -> bool:
        return (
            day == game_date
            and tuple(normalize_team(part) for part in pairing.split("@")) == matchup
            and normalize_team(team) == abbreviations[team_id]
        )

    statuses = [
        row.status
        for row in report.team_statuses
        if matches(row.game_date, row.matchup, row.team_abbreviation)
    ]
    if not statuses or any(status is not ReportSubmissionStatus.SUBMITTED for status in statuses):
        return set()
    allowed: set[str] = set()
    pending: set[str] = set()
    for entry in report.entries:
        if not matches(entry.game_date, entry.matchup, entry.team_abbreviation):
            continue
        ids = names.get(normalize_report_player_name(entry.player_name), set())
        if len(ids) != 1:
            continue
        target = pending if "trade pending" in (entry.reason or "").casefold() else allowed
        target.update(ids)
    return allowed - pending


def _read_reports(
    path: Path,
) -> tuple[tuple[OfficialInjuryReportSnapshot, ...], tuple[SourceFingerprint, ...]]:
    """Verify the selected parsed caches against both their ledger and retained PDF bytes."""
    payload = path.read_bytes()
    ledger = _ReportLedger.model_validate_json(payload)
    fingerprints = {
        "injury-team-proxy-ledger": source_fingerprint(
            "injury-team-proxy-ledger", payload, version=ledger.schema_version
        )
    }
    snapshots = []
    for source in ledger.reports:
        cache_path = path.parent / source.path
        raw = cache_path.read_bytes()
        cache = _ReportCache.model_validate_json(raw)
        pdf = cache_path.with_suffix(".pdf").read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != source.sha256
            or not pdf.startswith(b"%PDF-")
            or hashlib.sha256(pdf).hexdigest() != cache.pdf_sha256
        ):
            raise HistoricalTeamWeekBundleError("Injury team evidence source hash mismatch")
        snapshot = deserialize_official_injury_report_snapshot(cache.snapshot)
        if (
            snapshot.published_at.tzinfo is None
            or snapshot.source.source_updated_at != snapshot.published_at
            or snapshot.source.content_hash != cache.pdf_sha256
        ):
            raise HistoricalTeamWeekBundleError(
                "Injury team evidence has inconsistent publication metadata"
            )
        snapshots.append(snapshot)
        for name, digest, version in (
            (f"injury-cache:{source.sha256}", source.sha256, "json-v2"),
            (f"injury-pdf:{cache.pdf_sha256}", cache.pdf_sha256, "pdf-v1"),
        ):
            fingerprints[name] = SourceFingerprint(name, digest, version)
    return tuple(snapshots), tuple(fingerprints[name] for name in sorted(fingerprints))
