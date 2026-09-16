"""Validate reviewed official final reports as scoped complete game-roster censuses.

The ledger declares which provider players were checked against each retained report.
Only that explicit cohort gains negative roster evidence when a player is absent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs
from sleeper_manager.backtesting.replay.inputs import SourceFingerprint, source_fingerprint
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus
from sleeper_manager.backtesting.replay.roster_timeline import EASTERN_TIME
from sleeper_manager.backtesting.replay.team_week_sources import HistoricalTeamWeekBundleError
from sleeper_manager.domain.nba import GameStatus


class _RosterReport(BaseModel):
    """Require a hashed final report and an explicit reviewed-player cohort."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    game_id: str = Field(min_length=1)
    game_start: AwareDatetime
    home_team_id: str = Field(min_length=1)
    away_team_id: str = Field(min_length=1)
    pdf_path: str = Field(min_length=1)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_url: str = Field(pattern=r"^https://statsdmz\.nba\.com/pdfs/\d{8}/\d{8}_[A-Z]+\.pdf$")
    page: int = Field(ge=1)
    retrieved_at: AwareDatetime
    reviewed_player_ids: tuple[str, ...]
    player_teams: tuple[tuple[str, str], ...]
    verification: str = Field(min_length=1)


class _RosterLedger(BaseModel):
    """Version the complete-roster review contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["reviewed-final-game-rosters-v1"]
    reports: tuple[_RosterReport, ...]


@dataclass(frozen=True, slots=True)
class FinalRosterEvidence:
    """Scoped game-roster censuses and immutable source fingerprints."""

    censuses: tuple[GameRosterCensus, ...] = ()
    source_fingerprints: tuple[SourceFingerprint, ...] = ()


def load_final_roster_evidence(
    path: Path, nba_inputs: HistoricalExperimentInputs
) -> FinalRosterEvidence:
    """Validate every reviewed cohort, association, final game and retained PDF."""

    payload = path.read_bytes()
    ledger = _RosterLedger.model_validate_json(payload)
    games = {game.provider_id: game for game in nba_inputs.games}
    provider_ids = {player.provider_id for player in nba_inputs.provider_players}
    fingerprints = {
        "reviewed-final-game-rosters": source_fingerprint(
            "reviewed-final-game-rosters", payload, version=ledger.schema_version
        )
    }
    censuses: list[GameRosterCensus] = []
    seen_games: set[str] = set()
    verified_pdfs: set[tuple[Path, str]] = set()
    for report in ledger.reports:
        if report.game_id in seen_games:
            raise HistoricalTeamWeekBundleError(
                f"Duplicate final game-roster review: {report.game_id}"
            )
        seen_games.add(report.game_id)
        game = games.get(report.game_id)
        if (
            game is None
            or game.status is not GameStatus.FINAL
            or game.start_time != report.game_start
            or (game.home_team_id, game.away_team_id) != (report.home_team_id, report.away_team_id)
            or report.retrieved_at < report.game_start
        ):
            raise HistoricalTeamWeekBundleError(
                f"Final game-roster review conflicts with game: {report.game_id}"
            )
        game_date = game.start_time.astimezone(EASTERN_TIME).strftime("%Y%m%d")
        if not report.report_url.startswith(
            f"https://statsdmz.nba.com/pdfs/{game_date}/{game_date}_"
        ):
            raise HistoricalTeamWeekBundleError(
                f"Final game-roster report belongs to another date: {report.game_id}"
            )
        if not set(report.reviewed_player_ids) <= provider_ids:
            raise HistoricalTeamWeekBundleError(
                f"Final game-roster review has an unknown player: {report.game_id}"
            )
        pdf_path = path.parent / report.pdf_path
        pdf_key = (pdf_path, report.pdf_sha256)
        if pdf_key not in verified_pdfs:
            pdf = pdf_path.read_bytes()
            if not pdf.startswith(b"%PDF-") or hashlib.sha256(pdf).hexdigest() != report.pdf_sha256:
                raise HistoricalTeamWeekBundleError(
                    f"Final game-roster PDF hash mismatch: {report.game_id}"
                )
            verified_pdfs.add(pdf_key)
        source = f"{report.report_url}#page={report.page}"
        censuses.append(
            GameRosterCensus(
                game_id=report.game_id,
                home_team_id=report.home_team_id,
                away_team_id=report.away_team_id,
                reviewed_player_ids=report.reviewed_player_ids,
                player_teams=report.player_teams,
                source=source,
            )
        )
        name = f"nba-final-roster-pdf:{report.pdf_sha256}"
        fingerprints[name] = SourceFingerprint(name, report.pdf_sha256, "pdf-v1")
    return FinalRosterEvidence(
        tuple(sorted(censuses, key=lambda census: census.game_id)),
        tuple(fingerprints[name] for name in sorted(fingerprints)),
    )


__all__ = ("FinalRosterEvidence", "load_final_roster_evidence")
