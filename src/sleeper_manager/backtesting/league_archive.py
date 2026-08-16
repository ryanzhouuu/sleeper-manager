from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.sleeper.schemas import (
    SleeperLeaguePayload,
    SleeperPlayerPayload,
    SleeperRosterPayload,
    SleeperTransactionPayload,
)


class LeagueArchiveError(ValueError):
    """Raised when an archived Sleeper input cannot support exact replay."""


class SleeperArchiveReader(Protocol):
    async def league(self, league_id: str) -> dict[str, Any]: ...

    async def rosters(self, league_id: str) -> list[dict[str, Any]]: ...

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]: ...

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]: ...

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ArchivedRoster:
    roster_id: int
    player_ids: tuple[str, ...]
    starter_ids: tuple[str, ...] = ()
    reserve_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoricalMatchup:
    week: int
    roster_id: int
    player_ids: tuple[str, ...]
    starter_ids: tuple[str, ...] = ()
    points: float | None = None
    leg: int | None = None


@dataclass(frozen=True, slots=True)
class HistoricalTransaction:
    transaction_id: str
    transaction_type: str
    status: str
    leg: int | None
    created_at: datetime | None
    status_updated_at: datetime | None
    adds: tuple[tuple[str, int], ...]
    drops: tuple[tuple[str, int], ...]

    @property
    def is_complete(self) -> bool:
        return self.status.casefold() == "complete"

    @property
    def effective_at(self) -> datetime | None:
        return self.status_updated_at


@dataclass(frozen=True, slots=True)
class PlayerEligibilitySnapshot:
    sleeper_id: str
    eligible_positions: tuple[str, ...]
    available_as_of: datetime
    source: str
    confidence: str


@dataclass(frozen=True, slots=True)
class ArchiveSourceArtifact:
    resource: str
    path: str
    content_hash: str
    byte_count: int
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class HistoricalLeagueArchive:
    league_id: str
    resolved_from_league_id: str | None
    season: str
    retrieved_at: datetime
    scoring_policy: ScoringPolicy
    roster_slots: tuple[str, ...]
    total_rosters: int
    final_rosters: tuple[ArchivedRoster, ...]
    matchup_weeks: tuple[HistoricalMatchup, ...]
    transactions: tuple[HistoricalTransaction, ...]
    player_eligibility: tuple[PlayerEligibilitySnapshot, ...]
    source_artifacts: tuple[ArchiveSourceArtifact, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise LeagueArchiveError("Archive retrieval time must be timezone-aware")
        if not self.league_id.strip() or not self.season.strip():
            raise LeagueArchiveError("Archive league ID and season are required")
        if self.total_rosters <= 0:
            raise LeagueArchiveError("Archive must contain at least one roster")

    @property
    def matchups_by_week(self) -> dict[int, tuple[HistoricalMatchup, ...]]:
        result: dict[int, list[HistoricalMatchup]] = {}
        for matchup in self.matchup_weeks:
            result.setdefault(matchup.week, []).append(matchup)
        return {week: tuple(values) for week, values in result.items()}


def parse_historical_league_archive(
    league_payload: Mapping[str, Any],
    *,
    rosters: Iterable[Mapping[str, Any]] = (),
    matchup_weeks: Mapping[int, Iterable[Mapping[str, Any]]] | None = None,
    transactions: Iterable[Mapping[str, Any]] = (),
    player_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    retrieved_at: datetime,
    resolved_from_league_id: str | None = None,
    source_artifacts: Iterable[ArchiveSourceArtifact] = (),
) -> HistoricalLeagueArchive:
    if retrieved_at.tzinfo is None:
        raise LeagueArchiveError("Archive retrieval time must be timezone-aware")
    try:
        league = SleeperLeaguePayload.model_validate(league_payload)
    except Exception as error:
        raise LeagueArchiveError(f"Invalid archived league payload: {error}") from error
    try:
        scoring = ScoringPolicy.from_sleeper(league.scoring_settings)
    except ValueError as error:
        raise LeagueArchiveError(str(error)) from error

    final_rosters = tuple(_parse_roster(payload) for payload in rosters)
    if len({roster.roster_id for roster in final_rosters}) != len(final_rosters):
        raise LeagueArchiveError("Archived rosters contain duplicate roster IDs")
    matchups = _parse_matchups(matchup_weeks or {})
    parsed_transactions = tuple(_parse_transaction(payload) for payload in transactions)
    eligibility = _parse_eligibility(player_catalog or {}, retrieved_at)
    fingerprint_payload = {
        "league_id": league.league_id,
        "season": league.season,
        "season_type": league.season_type,
        "total_rosters": league.total_rosters,
        "roster_positions": league.roster_positions,
        "scoring_policy": scoring.fingerprint,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HistoricalLeagueArchive(
        league_id=league.league_id,
        resolved_from_league_id=resolved_from_league_id,
        season=league.season,
        retrieved_at=retrieved_at.astimezone(UTC),
        scoring_policy=scoring,
        roster_slots=tuple(slot.strip().upper() for slot in league.roster_positions),
        total_rosters=league.total_rosters,
        final_rosters=final_rosters,
        matchup_weeks=matchups,
        transactions=parsed_transactions,
        player_eligibility=eligibility,
        source_artifacts=tuple(source_artifacts),
        configuration_fingerprint=fingerprint,
    )


def resolve_predecessor_chain(
    requested_league_id: str,
    league_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the requested league followed by its predecessor chain."""

    chain: list[str] = []
    current: str | None = requested_league_id
    while current is not None:
        if current in chain:
            raise LeagueArchiveError("Sleeper league predecessor chain contains a cycle")
        payload = league_payloads.get(current)
        if payload is None:
            raise LeagueArchiveError(f"Missing predecessor payload for league {current!r}")
        chain.append(current)
        previous = payload.get("previous_league_id")
        current = str(previous) if previous not in (None, "") else None
    return tuple(chain)


def atomic_write_json(path: Path, payload: Any) -> str:
    """Write a canonical archive payload and return its content hash."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temp:
        temp.write(encoded)
        temp.flush()
        os.fsync(temp.fileno())
        temporary_path = Path(temp.name)
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


async def acquire_sleeper_archive(
    reader: SleeperArchiveReader,
    *,
    league_id: str,
    root: Path,
    weeks: Iterable[int],
    retrieved_at: datetime,
) -> tuple[ArchiveSourceArtifact, ...]:
    """Acquire raw provider payloads without mutating an existing cache entry."""

    if retrieved_at.tzinfo is None:
        raise LeagueArchiveError("Archive retrieval time must be timezone-aware")
    archive_root = root / league_id
    payloads: list[tuple[str, Path, Any]] = [
        ("league", archive_root / "league.json", await reader.league(league_id)),
        ("rosters", archive_root / "rosters.json", await reader.rosters(league_id)),
        ("players", archive_root / "players.json", await reader.players(active=False)),
    ]
    for week in sorted(set(weeks)):
        if week <= 0:
            raise LeagueArchiveError("Archive weeks must be positive")
        week_root = archive_root / "weeks" / f"{week:02d}"
        payloads.extend(
            (
                (
                    f"week-{week}-matchups",
                    week_root / "matchups.json",
                    await reader.matchups(league_id, week),
                ),
                (
                    f"week-{week}-transactions",
                    week_root / "transactions.json",
                    await reader.transactions(league_id, week),
                ),
            )
        )
    artifacts: list[ArchiveSourceArtifact] = []
    for resource, path, payload in payloads:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if path.is_file() and path.read_bytes() != encoded:
            raise LeagueArchiveError(f"Refusing to overwrite immutable cached artifact: {path}")
        if not path.is_file():
            content_hash = atomic_write_json(path, payload)
        else:
            content_hash = hashlib.sha256(encoded).hexdigest()
        artifacts.append(
            ArchiveSourceArtifact(
                resource, str(path), content_hash, len(encoded), retrieved_at.astimezone(UTC)
            )
        )
    manifest = {
        "league_id": league_id,
        "retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
        "artifacts": [
            {
                "resource": artifact.resource,
                "path": artifact.path,
                "sha256": artifact.content_hash,
                "byte_count": artifact.byte_count,
            }
            for artifact in artifacts
        ],
    }
    atomic_write_json(archive_root / "manifest.json", manifest)
    return tuple(artifacts)


def _parse_roster(payload: Mapping[str, Any]) -> ArchivedRoster:
    try:
        roster = SleeperRosterPayload.model_validate(payload)
    except Exception as error:
        raise LeagueArchiveError(f"Invalid archived roster payload: {error}") from error
    return ArchivedRoster(
        roster_id=roster.roster_id,
        player_ids=_ids(roster.players),
        starter_ids=_ids(roster.starters),
        reserve_ids=_ids(roster.reserve or ()),
    )


def _parse_matchups(
    values: Mapping[int, Iterable[Mapping[str, Any]]],
) -> tuple[HistoricalMatchup, ...]:
    result: list[HistoricalMatchup] = []
    for week, payloads in values.items():
        if week <= 0:
            raise LeagueArchiveError("Archived matchup weeks must be positive")
        for payload in payloads:
            try:
                roster_id = int(payload["roster_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise LeagueArchiveError("Archived matchup is missing roster_id") from error
            result.append(
                HistoricalMatchup(
                    week=week,
                    roster_id=roster_id,
                    player_ids=_ids(payload.get("players", ())),
                    starter_ids=_ids(payload.get("starters", ())),
                    points=_optional_float(payload.get("points")),
                    leg=_optional_int(payload.get("leg")),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.week, item.roster_id)))


def _parse_transaction(payload: Mapping[str, Any]) -> HistoricalTransaction:
    try:
        transaction = SleeperTransactionPayload.model_validate(payload)
    except Exception as error:
        raise LeagueArchiveError(f"Invalid archived transaction payload: {error}") from error
    return HistoricalTransaction(
        transaction_id=transaction.transaction_id,
        transaction_type=transaction.type,
        status=transaction.status,
        leg=transaction.leg,
        created_at=_parse_timestamp(transaction.created),
        status_updated_at=_parse_timestamp(transaction.status_updated),
        adds=_changes(transaction.adds),
        drops=_changes(transaction.drops),
    )


def _parse_eligibility(
    player_catalog: Mapping[str, Mapping[str, Any]], retrieved_at: datetime
) -> tuple[PlayerEligibilitySnapshot, ...]:
    result: list[PlayerEligibilitySnapshot] = []
    for sleeper_id, payload in sorted(player_catalog.items()):
        try:
            player = SleeperPlayerPayload.model_validate(payload)
        except Exception as error:
            raise LeagueArchiveError(
                f"Invalid archived player payload for {sleeper_id!r}"
            ) from error
        positions = tuple(
            sorted({position.strip().upper() for position in player.fantasy_positions if position})
        )
        result.append(
            PlayerEligibilitySnapshot(
                sleeper_id=str(sleeper_id),
                eligible_positions=positions,
                available_as_of=retrieved_at.astimezone(UTC),
                source="sleeper",
                confidence="current_catalog",
            )
        )
    return tuple(result)


def _changes(values: Mapping[str, int | str] | None) -> tuple[tuple[str, int], ...]:
    if not values:
        return ()
    try:
        return tuple(sorted((str(player), int(roster)) for player, roster in values.items()))
    except (TypeError, ValueError) as error:
        raise LeagueArchiveError("Archived transaction roster IDs must be integers") from error


def _ids(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip() and str(value) != "0")


def _parse_timestamp(value: int | float | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if not isinstance(value, str):
        raise LeagueArchiveError(f"Invalid Sleeper timestamp {value!r}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise LeagueArchiveError(f"Invalid Sleeper timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise LeagueArchiveError("Archived transaction timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise LeagueArchiveError(f"Expected integer value, got {value!r}") from error


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise LeagueArchiveError(f"Expected numeric value, got {value!r}") from error


__all__ = (
    "ArchivedRoster",
    "ArchiveSourceArtifact",
    "HistoricalLeagueArchive",
    "HistoricalMatchup",
    "HistoricalTransaction",
    "LeagueArchiveError",
    "PlayerEligibilitySnapshot",
    "SleeperArchiveReader",
    "atomic_write_json",
    "acquire_sleeper_archive",
    "parse_historical_league_archive",
    "resolve_predecessor_chain",
)
