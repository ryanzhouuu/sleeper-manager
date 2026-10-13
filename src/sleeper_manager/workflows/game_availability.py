"""Bind official report evidence to resolved players and unique scheduled games.

Generic provider injury designations are deliberately excluded from this boundary.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol

from sleeper_manager.domain.nba import AvailabilityStatus, ProviderPlayer, ScheduledGame
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.nba.official_injury_mapping import map_official_injury_report
from sleeper_manager.integrations.nba.official_injury_models import (
    EASTERN_TIME,
    OfficialInjuryReportSnapshot,
    ReportSubmissionStatus,
)
from sleeper_manager.projections.hybrid_types import GameAvailability, require_aware


@dataclass(frozen=True, slots=True)
class GameAvailabilityEvidence:
    """Provider-player/game assignments and explicit source or matching gaps."""

    assignments: tuple[tuple[str, GameAvailability], ...] = ()
    warnings: tuple[str, ...] = ()


class OfficialAvailabilitySource(Protocol):
    """Provide a cutoff-visible report; collection captures its completed retrieval time."""

    async def report(self, cutoff: datetime) -> OfficialInjuryReportSnapshot | None: ...


async def collect_official_game_evidence(
    source: OfficialAvailabilitySource | None,
    *,
    players: tuple[ProviderPlayer, ...],
    games: tuple[ScheduledGame, ...],
    clock: Callable[[], datetime],
    max_age: timedelta,
) -> tuple[datetime, GameAvailabilityEvidence]:
    """Keep source failures explicit without replacing valid control projections."""
    if source is None:
        return clock(), GameAvailabilityEvidence()
    try:
        snapshot = await source.report(clock())
    except Exception as error:
        return clock(), GameAvailabilityEvidence(
            warnings=(f"official_availability_failed:{type(error).__name__}",)
        )
    cutoff = clock()
    if snapshot is None:
        return cutoff, GameAvailabilityEvidence(warnings=("official_availability_missing",))
    return cutoff, bind_official_game_availability(
        snapshot,
        players=players,
        games=games,
        cutoff=cutoff,
        max_age=max_age,
    )


def bind_official_game_availability(
    snapshot: OfficialInjuryReportSnapshot,
    *,
    players: tuple[ProviderPlayer, ...],
    games: tuple[ScheduledGame, ...],
    cutoff: datetime,
    max_age: timedelta,
) -> GameAvailabilityEvidence:
    """Require current retrieval, pre-cutoff publication, and exact team/date/time scope."""
    require_aware(cutoff)
    require_aware(snapshot.published_at)
    require_aware(snapshot.source.retrieved_at)
    if max_age <= timedelta(0):
        raise ValueError("Availability freshness must be positive")
    if snapshot.source.retrieved_at > cutoff or snapshot.published_at > cutoff:
        return GameAvailabilityEvidence(warnings=("future_official_availability",))
    if cutoff - snapshot.source.retrieved_at > max_age:
        return GameAvailabilityEvidence(warnings=("stale_official_availability",))
    if not snapshot.source.content_hash:
        return GameAvailabilityEvidence(warnings=("unverified_official_availability",))
    return _bind_game_report(
        snapshot,
        players=players,
        games=games,
        cutoff=cutoff,
        observed_at=snapshot.source.retrieved_at,
        source_kind="official",
        confirm_absences=True,
    )


def bind_reconstructed_game_availability(
    snapshot: OfficialInjuryReportSnapshot,
    *,
    players: tuple[ProviderPlayer, ...],
    games: tuple[ScheduledGame, ...],
    cutoff: datetime,
    max_publication_age: timedelta = timedelta(hours=24),
) -> GameAvailabilityEvidence:
    """Group historical outcomes by published reports, explicitly distinct from live captures."""
    require_aware(cutoff)
    require_aware(snapshot.published_at)
    require_aware(snapshot.source.retrieved_at)
    if max_publication_age <= timedelta(0):
        raise ValueError("Historical report age must be positive")
    if snapshot.published_at > cutoff:
        return GameAvailabilityEvidence(warnings=("future_historical_report",))
    if cutoff - snapshot.published_at > max_publication_age:
        return GameAvailabilityEvidence(warnings=("stale_historical_report",))
    if not snapshot.source.content_hash:
        return GameAvailabilityEvidence(warnings=("unverified_historical_report",))
    return _bind_game_report(
        snapshot,
        players=players,
        games=games,
        cutoff=cutoff,
        observed_at=snapshot.published_at,
        source_kind=f"reconstructed-official:archive-retrieved:{snapshot.source.retrieved_at.isoformat()}",
        confirm_absences=False,
    )


def _bind_game_report(
    snapshot: OfficialInjuryReportSnapshot,
    *,
    players: tuple[ProviderPlayer, ...],
    games: tuple[ScheduledGame, ...],
    cutoff: datetime,
    observed_at: datetime,
    source_kind: str,
    confirm_absences: bool,
) -> GameAvailabilityEvidence:
    """Share identity and game-scope checks across declared live and retrospective semantics."""
    mapped = map_official_injury_report(snapshot, players)
    warnings = list(mapped.warnings)
    unavailable_teams = {normalize_team(row.entry.team_abbreviation) for row in mapped.unresolved}
    by_player = {player.provider_id: player for player in players}
    assignments: dict[tuple[str, str], GameAvailability] = {}
    conflicted: set[tuple[str, str]] = set()
    source_version = (
        f"{source_kind}:{snapshot.source.content_hash}:{snapshot.published_at.isoformat()}"
    )
    for player in players:
        for game in games:
            if (
                player.team_id not in (game.home_team_id, game.away_team_id)
                or game.start_time <= cutoff
            ):
                continue
            local = game.start_time.astimezone(EASTERN_TIME)
            team = normalize_team(player.team_abbreviation)
            statuses = tuple(
                row
                for row in snapshot.team_statuses
                if normalize_team(row.team_abbreviation) == team
                and row.game_date == local.date()
                and _matches_report_clock(row.game_time, local.time().replace(tzinfo=None))
                and team in {normalize_team(part) for part in row.matchup.split("@")}
                and len(
                    {
                        candidate.provider_id
                        for candidate in games
                        if player.team_id in (candidate.home_team_id, candidate.away_team_id)
                        and candidate.start_time.astimezone(EASTERN_TIME).date() == row.game_date
                        and _matches_report_clock(
                            row.game_time,
                            candidate.start_time.astimezone(EASTERN_TIME)
                            .time()
                            .replace(tzinfo=None),
                        )
                    }
                )
                == 1
            )
            if len(statuses) != 1:
                warnings.append(
                    f"official_game_scope_unresolved:{player.provider_id}:{game.provider_id}"
                )
                continue
            status = statuses[0]
            rows = tuple(
                row
                for row in mapped.availability
                if row.player_id == player.provider_id
                and row.game_date == status.game_date
                and row.game_time == status.game_time
                and row.matchup == status.matchup
                and normalize_team(row.team_abbreviation) == team
                and normalize_team(by_player[row.player_id].team_abbreviation) == team
            )
            if len(rows) > 1:
                warnings.append(
                    f"conflicting_official_availability:{player.provider_id}:{game.provider_id}"
                )
                continue
            if status.status is ReportSubmissionStatus.NOT_YET_SUBMITTED:
                observation, designation = (
                    AvailabilityObservation.MISSING_REPORT,
                    AvailabilityStatus.UNKNOWN,
                )
            elif rows:
                observation, designation = AvailabilityObservation.REPORTED, rows[0].status
            elif team in unavailable_teams:
                warnings.append(
                    f"unresolved_official_identity:{player.provider_id}:{game.provider_id}"
                )
                continue
            else:
                observation, designation = (
                    AvailabilityObservation.NOT_LISTED,
                    AvailabilityStatus.UNKNOWN,
                )
            report = GameAvailability(
                game.provider_id,
                observed_at,
                designation,
                observation,
                source_version,
                confirmed_absence=confirm_absences and designation is AvailabilityStatus.OUT,
            )
            key = player.provider_id, game.provider_id
            if key in assignments and assignments[key] != report:
                conflicted.add(key)
            assignments[key] = report
    return GameAvailabilityEvidence(
        tuple(
            (player, report)
            for (player, game), report in sorted(assignments.items())
            if (player, game) not in conflicted
        ),
        tuple(sorted(set(warnings))),
    )


def _matches_report_clock(reported: time, scheduled: time) -> bool:
    """Official PDFs omit AM/PM; the scheduled game resolves that ambiguity."""
    return reported == scheduled or (
        1 <= reported.hour <= 12
        and reported.hour % 12 == scheduled.hour % 12
        and (reported.minute, reported.second) == (scheduled.minute, scheduled.second)
    )
