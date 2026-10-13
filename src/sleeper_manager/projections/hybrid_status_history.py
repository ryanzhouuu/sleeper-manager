"""Attach reconstructed pregame status evidence to an existing resolved participation census."""

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from hashlib import sha256

from sleeper_manager.backtesting.replay.inputs import SourceFingerprint
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus
from sleeper_manager.domain.nba import ProviderPlayer, ScheduledGame, Team
from sleeper_manager.integrations.nba.official_injury_models import OfficialInjuryReportSnapshot
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_types import HybridHistory, ParticipationOpportunity
from sleeper_manager.workflows.game_availability import bind_reconstructed_game_availability


@dataclass(frozen=True, slots=True)
class StatusHistoryGap:
    """Retain unmatched opportunities without changing their resolved outcome."""

    game_id: str
    player_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class StatusHistoryBuild:
    """Status tagging changes dataset identity, never the stat sample or census denominator."""

    history: HybridHistory
    added_count: int
    gaps: tuple[StatusHistoryGap, ...]
    sources: tuple[SourceFingerprint, ...]
    cutoff_minutes: int

    @property
    def coverage(self) -> dict[str, object]:
        return {
            "schema_version": "hybrid-status-coverage-v1",
            "dataset_version": self.history.dataset_version,
            "evidence_semantics": "reconstructed_official_publication_not_prospective_capture",
            "cutoff_minutes": self.cutoff_minutes,
            "maximum_publication_age_hours": 24,
            "observation_count": len(self.history.observations),
            "opportunity_count": len(self.history.opportunities),
            "added_count": self.added_count,
            "status_tagged_count": sum(
                row.availability is not None for row in self.history.opportunities
            ),
            "buckets": dict(
                sorted(
                    Counter(
                        row.availability.bucket
                        for row in self.history.opportunities
                        if row.availability is not None
                    ).items()
                )
            ),
            "gap_counts": dict(sorted(Counter(row.reason for row in self.gaps).items())),
            "gaps": tuple(asdict(row) for row in self.gaps),
            "sources": tuple(asdict(row) for row in self.sources),
        }


def attach_hybrid_status_history(
    history: HybridHistory,
    *,
    games: tuple[ScheduledGame, ...],
    players: tuple[ProviderPlayer, ...],
    teams: tuple[Team, ...],
    censuses: tuple[GameRosterCensus, ...],
    reports: Mapping[str, OfficialInjuryReportSnapshot],
    sources: tuple[SourceFingerprint, ...],
    cutoff_minutes: int = 30,
) -> StatusHistoryBuild:
    """Use verified census membership and report scope; retain every untagged coverage gap."""
    if isinstance(cutoff_minutes, bool) or cutoff_minutes <= 0:
        raise HybridProjectionError("invalid_status_history_cutoff")
    by_game = {game.provider_id: game for game in games}
    by_census = {census.game_id: census for census in censuses}
    if len(by_game) != len(games) or len(by_census) != len(censuses):
        raise HybridProjectionError("duplicate_status_history_game")
    abbreviations = {team.provider_id: team.abbreviation for team in teams}
    by_player: dict[str, list[ProviderPlayer]] = defaultdict(list)
    for player in players:
        by_player[player.provider_id].append(player)
    grouped: dict[str, list[ParticipationOpportunity]] = defaultdict(list)
    for row in history.opportunities:
        grouped[row.game_id].append(row)
    assignments = {}
    gaps = []
    for game_id, rows in sorted(grouped.items()):
        game, census, report = by_game.get(game_id), by_census.get(game_id), reports.get(game_id)
        if game is not None and any(row.game_start != game.start_time for row in rows):
            raise HybridProjectionError("status_history_game_time_mismatch")
        if (
            game is not None
            and census is not None
            and (
                (game.home_team_id, game.away_team_id) != (census.home_team_id, census.away_team_id)
            )
        ):
            raise HybridProjectionError("status_history_census_mismatch")
        membership = dict(census.player_teams) if census is not None else {}
        scoped = tuple(
            replace(player, team_id=team, team_abbreviation=abbreviations[team])
            for player_id, team in sorted(membership.items())
            if team in abbreviations
            for player in by_player.get(player_id, ())
        )
        evidence = (
            bind_reconstructed_game_availability(
                report,
                players=scoped,
                games=(game,),
                cutoff=game.start_time - timedelta(minutes=cutoff_minutes),
            )
            if report is not None and game is not None
            else None
        )
        bound = (
            {(player, status.game_id): status for player, status in evidence.assignments}
            if evidence
            else {}
        )
        for row in rows:
            if row.availability is not None:
                continue
            reason = (
                "missing_game"
                if game is None
                else "missing_census"
                if census is None
                else "missing_membership"
                if row.player_id not in membership
                else "missing_player_identity"
                if row.player_id not in by_player
                else "missing_team_identity"
                if membership[row.player_id] not in abbreviations
                else "missing_report"
                if report is None
                else "report_not_applicable"
            )
            status = bound.get((row.player_id, game_id))
            if status is not None:
                assignments[game_id, row.player_id] = status
            else:
                gaps.append(StatusHistoryGap(game_id, row.player_id, reason))
    opportunities = tuple(
        replace(row, availability=assignments.get((row.game_id, row.player_id), row.availability))
        for row in history.opportunities
    )
    proofs = tuple(sorted(set(sources), key=lambda row: (row.name, row.content_hash, row.version)))
    dataset_version = history.dataset_version
    if assignments:
        payload = {
            "previous": history.dataset_version,
            "policy": "official-latest-pregame-v1",
            "cutoff_minutes": cutoff_minutes,
            "maximum_age_hours": 24,
            "opportunities": tuple(asdict(row) for row in opportunities),
            "sources": tuple(asdict(row) for row in proofs),
        }
        dataset_version = (
            "hybrid-status-history-v1-"
            + sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
        )
    result = HybridHistory(dataset_version, history.observations, opportunities)
    return StatusHistoryBuild(result, len(assignments), tuple(gaps), proofs, cutoff_minutes)
