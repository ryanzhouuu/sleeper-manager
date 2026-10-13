"""Bind official report evidence to resolved players and unique scheduled games.

Generic provider injury designations are deliberately excluded from this boundary.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    mapped = map_official_injury_report(snapshot, players)
    warnings = list(mapped.warnings)
    unavailable_teams = {normalize_team(row.entry.team_abbreviation) for row in mapped.unresolved}
    by_player = {player.provider_id: player for player in players}
    assignments: dict[tuple[str, str], GameAvailability] = {}
    conflicted: set[tuple[str, str]] = set()
    source_version = f"official:{snapshot.source.content_hash}:{snapshot.published_at.isoformat()}"
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
                and row.game_time == local.time().replace(tzinfo=None)
                and team in {normalize_team(part) for part in row.matchup.split("@")}
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
                snapshot.source.retrieved_at,
                designation,
                observation,
                source_version,
                confirmed_absence=designation is AvailabilityStatus.OUT,
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
