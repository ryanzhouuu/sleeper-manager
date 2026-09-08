"""Validate reviewed final inactive evidence without inferring outcomes from missing rows.

The review ledger binds explicit player/game associations to retained final PDFs.
Reviewers establish those associations; this importer verifies their integrity and
compatibility with the existing NBA evidence, without parsing or fetching PDFs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from sleeper_manager.backtesting.experiments.data import HistoricalExperimentInputs
from sleeper_manager.backtesting.replay.inputs import SourceFingerprint, source_fingerprint
from sleeper_manager.backtesting.replay.roster_timeline import EASTERN_TIME
from sleeper_manager.backtesting.replay.team_week_sources import HistoricalTeamWeekBundleError
from sleeper_manager.domain.nba import GameStatus, PlayerBoxScore, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.mapping import normalize_player_name


class _InactiveRecord(BaseModel):
    """Require a reviewed final result, never a pregame availability prediction."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    player_id: str = Field(min_length=1)
    player_name: str = Field(min_length=1)
    game_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    game_start: AwareDatetime
    status: Literal["confirmed_final_inactive"]
    pdf_path: str = Field(min_length=1)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_url: str = Field(pattern=r"^https://statsdmz\.nba\.com/pdfs/\d{8}/\d{8}_[A-Z]+\.pdf$")
    page: int = Field(ge=1)
    retrieved_at: AwareDatetime
    verification: str = Field(min_length=1)


class _InactiveLedger(BaseModel):
    """Version the review contract and reject undeclared evidence fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["reviewed-final-inactive-v1"]
    records: tuple[_InactiveRecord, ...]


@dataclass(frozen=True, slots=True)
class InactiveEvidence:
    """Supplemental outcomes and the immutable inputs backing their review."""

    box_scores: tuple[PlayerBoxScore, ...] = ()
    source_fingerprints: tuple[SourceFingerprint, ...] = ()


def load_inactive_evidence(path: Path, nba_inputs: HistoricalExperimentInputs) -> InactiveEvidence:
    """Validate the complete ledger; missing, malformed or conflicting evidence raises.

    PDF paths are relative to the ledger unless absolute. The returned boxes are
    scoring supplements, not new team observations or projection history.
    """
    payload = path.read_bytes()
    ledger = _InactiveLedger.model_validate_json(payload)
    games = {game.provider_id: game for game in nba_inputs.games}
    names: dict[str, set[str]] = {}
    for player in nba_inputs.provider_players:
        names.setdefault(player.provider_id, set()).add(normalize_player_name(player.full_name))
    existing: dict[tuple[str, str], list[PlayerBoxScore]] = {}
    for box in nba_inputs.player_box_scores:
        existing.setdefault((box.game_id, box.player_id), []).append(box)
    fingerprints = {
        "reviewed-final-inactive": source_fingerprint(
            "reviewed-final-inactive", payload, version=ledger.schema_version
        )
    }
    boxes: list[PlayerBoxScore] = []
    seen: set[tuple[str, str]] = set()
    verified_pdfs: set[tuple[Path, str]] = set()
    pdf_games: dict[str, str] = {}
    for record in ledger.records:
        key = (record.game_id, record.player_id)
        if key in seen:
            raise HistoricalTeamWeekBundleError(f"Duplicate inactive evidence: {key}")
        seen.add(key)
        game = games.get(record.game_id)
        if (
            game is None
            or game.status is not GameStatus.FINAL
            or game.start_time != record.game_start
            or record.team_id not in (game.home_team_id, game.away_team_id)
            or record.retrieved_at < record.game_start
        ):
            raise HistoricalTeamWeekBundleError(f"Inactive evidence conflicts with game: {key}")
        if names.get(record.player_id) != {normalize_player_name(record.player_name)}:
            raise HistoricalTeamWeekBundleError(f"Inactive evidence has unresolved identity: {key}")
        game_date = game.start_time.astimezone(EASTERN_TIME).strftime("%Y%m%d")
        if (
            not record.report_url.startswith(
                f"https://statsdmz.nba.com/pdfs/{game_date}/{game_date}_"
            )
            or pdf_games.setdefault(record.pdf_sha256, record.game_id) != record.game_id
        ):
            raise HistoricalTeamWeekBundleError(
                f"Inactive report belongs to a different game: {key}"
            )
        pdf_path = path.parent / record.pdf_path
        pdf_key = (pdf_path, record.pdf_sha256)
        if pdf_key not in verified_pdfs:
            pdf = pdf_path.read_bytes()
            if not pdf.startswith(b"%PDF-") or hashlib.sha256(pdf).hexdigest() != record.pdf_sha256:
                raise HistoricalTeamWeekBundleError(f"Inactive evidence PDF hash mismatch: {key}")
            verified_pdfs.add(pdf_key)
        source = SourceMetadata(
            provider="nba_official_final_inactive",
            provider_id=f"{record.report_url}#page={record.page}",
            retrieved_at=record.retrieved_at,
            schema_version=ledger.schema_version,
            content_hash=record.pdf_sha256,
        )
        box = PlayerBoxScore(
            game_id=record.game_id,
            player_id=record.player_id,
            team_id=record.team_id,
            played_at=game.start_time,
            started=False,
            did_play=False,
            minutes=0,
            line=BoxScoreLine(),
            source=source,
        )
        if prior_boxes := existing.get(key):
            if len(prior_boxes) != 1:
                raise HistoricalTeamWeekBundleError(
                    f"Inactive evidence has duplicate outcomes: {key}"
                )
            previous = prior_boxes[0]
            if (
                previous.did_play
                or previous.started
                or previous.minutes not in (None, 0)
                or previous.line != BoxScoreLine()
                or previous.team_id != record.team_id
            ):
                raise HistoricalTeamWeekBundleError(
                    f"Inactive evidence conflicts with outcome: {key}"
                )
            box = replace(previous, additional_sources=(*previous.additional_sources, source))
        boxes.append(box)
        name = f"nba-final-pdf:{record.pdf_sha256}"
        fingerprints[name] = SourceFingerprint(name, record.pdf_sha256, "pdf-v1")
    return InactiveEvidence(
        tuple(sorted(boxes, key=lambda box: (box.game_id, box.player_id))),
        tuple(fingerprints[name] for name in sorted(fingerprints)),
    )
