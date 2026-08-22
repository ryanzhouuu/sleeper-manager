from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sleeper_manager.backtesting.replay.inputs.manifest import build_replay_input_manifest
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalReplayBuildInput,
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
    ReplayInputExclusion,
    ReplayInputManifest,
)
from sleeper_manager.backtesting.replay.league_archive import (
    HistoricalMatchup,
    PlayerEligibilitySnapshot,
)
from sleeper_manager.backtesting.replay.models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
)
from sleeper_manager.backtesting.replay.roster_timeline import (
    FantasyWeekBoundary,
    RosterMembershipInterval,
    RosterTimeline,
    assign_game_to_week,
)
from sleeper_manager.domain.nba import GameStatus, PlayerBoxScore, ScheduledGame
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.scoring import calculate_fantasy_points
from sleeper_manager.integrations.nba.identity import PlayerMapping


@dataclass(frozen=True, slots=True)
class _JoinedPlayerGame:
    sleeper_id: str
    mapping: PlayerMapping
    game: ScheduledGame
    box_score: PlayerBoxScore
    roster_id: int
    membership_segment: str | None


def assemble_historical_team_week_inputs(
    inputs: HistoricalReplayBuildInput,
    *,
    manifest: ReplayInputManifest | None = None,
    weeks: Sequence[int] | None = None,
    roster_ids: Sequence[int] | None = None,
) -> tuple[HistoricalTeamWeekInput, ...]:
    """Join immutable league, roster, schedule, and box-score evidence by tipoff."""

    replay_manifest = manifest or build_replay_input_manifest(inputs)
    boundaries = tuple(
        boundary
        for boundary in inputs.week_boundaries
        if weeks is None or boundary.week in set(weeks)
    )
    selected_rosters = _roster_ids(inputs, roster_ids)
    schedule_by_id, schedule_issues = _index_schedules(inputs.games)
    box_scores, box_score_issues = _index_box_scores(inputs.box_scores)
    mapping_by_provider, mapping_issues = _index_mappings(inputs.player_mappings)
    projection_by_key = {
        (snapshot.player_id, snapshot.game_id): snapshot
        for snapshot in inputs.projection_snapshots
    }
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]] = defaultdict(list)

    games_by_week: dict[int, tuple[ReplayGame, ...]] = {}
    for boundary in boundaries:
        games_by_week[boundary.week] = tuple(
            _replay_game(game, boundary.week)
            for game in schedule_by_id.values()
            if boundary.utc_start <= game.start_time < boundary.utc_end
        )

    for issue in schedule_issues:
        _add_game_issue(
            issues_by_scope,
            issue,
            _game_id_from_scope(issue.scope),
            schedule_by_id,
            boundaries,
            selected_rosters,
        )
    for issue in box_score_issues:
        _add_box_issue(
            issues_by_scope,
            issue,
            _game_id_from_scope(issue.scope),
            _provider_id_from_scope(issue.scope),
            schedule_by_id,
            mapping_by_provider,
            inputs,
            boundaries,
            selected_rosters,
        )
    for issue in mapping_issues:
        sleeper_id = _sleeper_id_from_scope(issue.scope)
        if sleeper_id is not None:
            _add_player_issue(
                issues_by_scope,
                issue,
                sleeper_id,
                inputs.roster_timeline,
                boundaries,
                selected_rosters,
            )
            continue
        provider_id = _provider_id_from_scope(issue.scope)
        if provider_id is not None:
            for box_score in inputs.box_scores:
                if box_score.player_id == provider_id:
                    _add_box_issue(
                        issues_by_scope,
                        issue,
                        box_score.game_id,
                        provider_id,
                        schedule_by_id,
                        mapping_by_provider,
                        inputs,
                        boundaries,
                        selected_rosters,
                    )

    joined_by_scope: dict[tuple[int, int], list[_JoinedPlayerGame]] = defaultdict(list)
    for box_score in box_scores:
        game = schedule_by_id.get(box_score.game_id)
        if game is None:
            _add_box_issue(
                issues_by_scope,
                ReplayInputExclusion(
                    PlanningReasonCode.MISSING_GAME_SCHEDULE,
                    f"game={box_score.game_id}",
                    "Player box score has no matching historical schedule.",
                ),
                box_score.game_id,
                box_score.player_id,
                schedule_by_id,
                mapping_by_provider,
                inputs,
                boundaries,
                selected_rosters,
            )
            continue
        game_week = assign_game_to_week(game.start_time, boundaries)
        if game_week is None:
            continue
        mapping = mapping_by_provider.get(box_score.player_id)
        if mapping is None:
            _add_game_issue(
                issues_by_scope,
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"provider-player={box_score.player_id}",
                    "No Sleeper-to-provider identity mapping was supplied.",
                ),
                box_score.game_id,
                schedule_by_id,
                boundaries,
                selected_rosters,
            )
            continue
        sleeper_id = mapping.sleeper_id
        for roster_id in selected_rosters:
            interval = _membership_interval(
                inputs.roster_timeline, roster_id, sleeper_id, game.start_time
            )
            if interval is None:
                continue
            joined_by_scope[(game_week.week, roster_id)].append(
                _JoinedPlayerGame(
                    sleeper_id,
                    mapping,
                    game,
                    box_score,
                    roster_id,
                    _membership_segment(interval),
                )
            )

    team_weeks: list[HistoricalTeamWeekInput] = []
    for boundary in boundaries:
        for roster_id in selected_rosters:
            scope = f"league={inputs.archive.league_id}:week={boundary.week}:roster={roster_id}"
            scope_issues = list(issues_by_scope.get((boundary.week, roster_id), ()))
            candidates = joined_by_scope.get((boundary.week, roster_id), [])
            player_games: list[ReplayPlayerGame] = []
            exact_count = 0
            best_known_count = 0
            scored_count = 0
            projected_count = 0
            missing_evidence: dict[PlanningReasonCode, int] = defaultdict(int)
            for candidate in candidates:
                positions, quality = _eligibility_at(
                    candidate.sleeper_id,
                    candidate.game.start_time,
                    inputs.eligibility_evidence or inputs.archive.player_eligibility,
                )
                if not positions:
                    reason = PlanningReasonCode.AMBIGUOUS_ELIGIBILITY
                    scope_issues.append(
                        ReplayInputExclusion(
                            reason,
                            scope,
                            "No unambiguous eligibility evidence exists at "
                            f"{candidate.game.start_time.isoformat()}.",
                        )
                    )
                    continue
                if quality is PlanningQuality.EXACT:
                    exact_count += 1
                else:
                    best_known_count += 1
                score = calculate_fantasy_points(candidate.box_score.line, inputs.scoring_policy)
                scored_count += 1
                projection = projection_by_key.get(
                    (candidate.sleeper_id, candidate.game.provider_id)
                )
                if projection is not None:
                    projected_count += 1
                else:
                    missing_evidence[PlanningReasonCode.MISSING_PROJECTION] += 1
                player_games.append(
                    ReplayPlayerGame(
                        sleeper_id=candidate.sleeper_id,
                        provider_player_id=candidate.mapping.espn_id or "",
                        game_id=candidate.game.provider_id,
                        fantasy_team_id=roster_id,
                        rostered_at_tipoff=True,
                        eligible_positions=positions,
                        actual_score=score,
                        projection=projection,
                        membership_segment=candidate.membership_segment,
                    )
                )

            for issue in scope_issues:
                missing_evidence[issue.reason] += 1
            excluded = _unique_exclusions(scope_issues)
            coverage = ReplayCoverageSummary(
                expected_player_games=len(candidates) + len(excluded),
                joined_player_games=len(candidates),
                resolved_identities=len(candidates),
                exact_eligibility=exact_count,
                best_known_eligibility=best_known_count,
                scored_player_games=scored_count,
                projected_player_games=projected_count,
                missing_evidence=tuple(
                    sorted(missing_evidence.items(), key=lambda item: item[0].value)
                ),
            )
            quality = _assembly_quality(coverage, scope_issues)
            if excluded:
                player_games = []
            team_weeks.append(
                HistoricalTeamWeekInput(
                    manifest_id=replay_manifest.manifest_id,
                    league_id=inputs.archive.league_id,
                    season=inputs.archive.season,
                    week=boundary.week,
                    roster_id=roster_id,
                    starter_slots=_starter_slots(inputs.archive.roster_slots),
                    roster_player_ids=_roster_players_for_week(
                        inputs.roster_timeline, roster_id, boundary
                    ),
                    observed_starter_ids=_observed_starters(
                        inputs.archive.matchup_weeks, boundary.week, roster_id
                    ),
                    games=games_by_week.get(boundary.week, ()),
                    player_games=tuple(player_games),
                    eligibility_quality=quality,
                    coverage=coverage,
                    exclusions=excluded,
                )
            )
    return tuple(team_weeks)


def _add_game_issue(
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]],
    issue: ReplayInputExclusion,
    game_id: str,
    schedule_by_id: dict[str, ScheduledGame],
    boundaries: Sequence[FantasyWeekBoundary],
    selected_rosters: Sequence[int],
) -> None:
    game = schedule_by_id.get(game_id)
    boundary = assign_game_to_week(game.start_time, boundaries) if game is not None else None
    if boundary is None:
        _add_unscoped_issue(issues_by_scope, issue, boundaries, selected_rosters)
        return
    _add_week_issue(issues_by_scope, issue, boundary.week, selected_rosters)


def _add_box_issue(
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]],
    issue: ReplayInputExclusion,
    game_id: str,
    provider_id: str | None,
    schedule_by_id: dict[str, ScheduledGame],
    mapping_by_provider: dict[str, PlayerMapping],
    inputs: HistoricalReplayBuildInput,
    boundaries: Sequence[FantasyWeekBoundary],
    selected_rosters: Sequence[int],
) -> None:
    game = schedule_by_id.get(game_id)
    at = game.start_time if game is not None else _box_score_time(inputs, game_id, provider_id)
    boundary = assign_game_to_week(at, boundaries) if at is not None else None
    if boundary is None:
        _add_unscoped_issue(issues_by_scope, issue, boundaries, selected_rosters)
        return
    mapping = mapping_by_provider.get(provider_id or "")
    if mapping is None:
        _add_week_issue(issues_by_scope, issue, boundary.week, selected_rosters)
        return
    _add_player_issue(
        issues_by_scope,
        issue,
        mapping.sleeper_id,
        inputs.roster_timeline,
        (boundary,),
        selected_rosters,
    )


def _add_player_issue(
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]],
    issue: ReplayInputExclusion,
    sleeper_id: str,
    timeline: RosterTimeline,
    boundaries: Sequence[FantasyWeekBoundary],
    selected_rosters: Sequence[int],
) -> None:
    for boundary in boundaries:
        roster_ids = tuple(
            roster_id
            for roster_id in selected_rosters
            if sleeper_id
            in timeline.players_overlapping(roster_id, boundary.utc_start, boundary.utc_end)
        )
        _add_week_issue(issues_by_scope, issue, boundary.week, roster_ids)


def _add_week_issue(
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]],
    issue: ReplayInputExclusion,
    week: int,
    roster_ids: Sequence[int],
) -> None:
    for roster_id in roster_ids:
        issues_by_scope[(week, roster_id)].append(
            ReplayInputExclusion(
                issue.reason,
                f"week={week}:roster={roster_id}:{issue.scope}",
                issue.detail,
            )
        )


def _add_unscoped_issue(
    issues_by_scope: dict[tuple[int, int], list[ReplayInputExclusion]],
    issue: ReplayInputExclusion,
    boundaries: Sequence[FantasyWeekBoundary],
    selected_rosters: Sequence[int],
) -> None:
    for boundary in boundaries:
        _add_week_issue(issues_by_scope, issue, boundary.week, selected_rosters)


def _box_score_time(
    inputs: HistoricalReplayBuildInput,
    game_id: str,
    provider_id: str | None,
) -> datetime | None:
    return next(
        (
            box_score.played_at
            for box_score in inputs.box_scores
            if box_score.game_id == game_id
            and (provider_id is None or box_score.player_id == provider_id)
            and box_score.played_at is not None
        ),
        None,
    )


def _game_id_from_scope(scope: str) -> str:
    return scope.removeprefix("game=").split(":", 1)[0]


def _provider_id_from_scope(scope: str) -> str | None:
    marker = "provider-player="
    if marker not in scope:
        return None
    return scope.split(marker, 1)[1].split(":", 1)[0]


def _sleeper_id_from_scope(scope: str) -> str | None:
    marker = "sleeper-player="
    if marker not in scope:
        return None
    return scope.split(marker, 1)[1].split(":", 1)[0]


def _index_schedules(
    games: Sequence[ScheduledGame],
) -> tuple[dict[str, ScheduledGame], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[str, ScheduledGame] = {}
    issues: list[ReplayInputExclusion] = []
    for game in games:
        previous = indexed.get(game.provider_id)
        if previous is None:
            indexed[game.provider_id] = game
            continue
        if previous != game:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={game.provider_id}",
                    "Historical schedule contains conflicting rows for one game ID.",
                )
            )
    return indexed, tuple(issues)


def _index_box_scores(
    box_scores: Sequence[PlayerBoxScore],
) -> tuple[tuple[PlayerBoxScore, ...], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[tuple[str, str], PlayerBoxScore] = {}
    issues: list[ReplayInputExclusion] = []
    for box_score in box_scores:
        key = box_score.game_id, box_score.player_id
        previous = indexed.get(key)
        if previous is None:
            indexed[key] = box_score
            continue
        if previous != box_score:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={box_score.game_id}:provider-player={box_score.player_id}",
                    "Historical box scores contain conflicting rows for one player-game.",
                )
            )
    return tuple(indexed.values()), tuple(issues)


def _index_mappings(
    mappings: Sequence[PlayerMapping],
) -> tuple[dict[str, PlayerMapping], tuple[ReplayInputExclusion, ...]]:
    by_provider: dict[str, list[PlayerMapping]] = defaultdict(list)
    issues: list[ReplayInputExclusion] = []
    for mapping in mappings:
        if mapping.espn_id is None:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"sleeper-player={mapping.sleeper_id}",
                    mapping.reason,
                )
            )
            continue
        by_provider[mapping.espn_id].append(mapping)

    resolved: dict[str, PlayerMapping] = {}
    for provider_id, candidates in by_provider.items():
        unique = tuple(dict.fromkeys(candidates))
        if len({candidate.sleeper_id for candidate in unique}) != 1:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"provider-player={provider_id}",
                    "Provider player ID maps to multiple Sleeper players.",
                )
            )
            continue
        resolved[provider_id] = unique[0]
    return resolved, tuple(issues)


def _replay_game(game: ScheduledGame, week: int) -> ReplayGame:
    status = {
        GameStatus.SCHEDULED: ReplayGameStatus.SCHEDULED,
        GameStatus.IN_PROGRESS: ReplayGameStatus.SCHEDULED,
        GameStatus.FINAL: ReplayGameStatus.FINAL,
        GameStatus.POSTPONED: ReplayGameStatus.POSTPONED,
        GameStatus.CANCELED: ReplayGameStatus.CANCELED,
        GameStatus.UNKNOWN: ReplayGameStatus.SCHEDULED,
    }[game.status]
    final_time = (
        game.start_time + timedelta(minutes=game.duration_minutes)
        if status is ReplayGameStatus.FINAL
        else None
    )
    return ReplayGame(
        game_id=game.provider_id,
        start_time=game.start_time,
        final_time=final_time,
        week=week,
        team_ids=(game.home_team_id, game.away_team_id),
        status=status,
    )


def _eligibility_at(
    sleeper_id: str,
    cutoff: datetime,
    evidence: Sequence[PlayerEligibilitySnapshot],
) -> tuple[tuple[str, ...], PlanningQuality]:
    snapshots = tuple(
        sorted(
            (snapshot for snapshot in evidence if snapshot.sleeper_id == sleeper_id),
            key=lambda snapshot: snapshot.available_as_of,
        )
    )
    exact = tuple(snapshot for snapshot in snapshots if snapshot.available_as_of <= cutoff)
    if exact:
        snapshot = exact[-1]
        same_time = tuple(
            item for item in exact if item.available_as_of == snapshot.available_as_of
        )
        positions = _consistent_positions(same_time)
        return positions, PlanningQuality.EXACT if positions else PlanningQuality.PARTIAL
    if snapshots:
        positions = _consistent_positions((snapshots[0],))
        return (
            positions,
            PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE if positions else PlanningQuality.PARTIAL,
        )
    return (), PlanningQuality.PARTIAL


def _consistent_positions(
    snapshots: Sequence[PlayerEligibilitySnapshot],
) -> tuple[str, ...]:
    position_sets = {
        tuple(
            sorted(
                {
                    position.strip().upper()
                    for position in snapshot.eligible_positions
                    if position.strip()
                }
            )
        )
        for snapshot in snapshots
    }
    if len(position_sets) != 1:
        return ()
    return next(iter(position_sets), ())


def _membership_interval(
    timeline: RosterTimeline,
    roster_id: int,
    sleeper_id: str,
    at: datetime,
) -> RosterMembershipInterval | None:
    matches = timeline.membership_intervals_at(roster_id, sleeper_id, at)
    return matches[0] if len(matches) == 1 else None


def _membership_segment(interval: RosterMembershipInterval) -> str:
    return (
        ":".join(interval.source_transaction_ids) if interval.source_transaction_ids else "initial"
    )


def _roster_ids(
    inputs: HistoricalReplayBuildInput,
    requested: Sequence[int] | None,
) -> tuple[int, ...]:
    discovered = {roster.roster_id for roster in inputs.archive.final_rosters}
    discovered.update(interval.roster_id for interval in inputs.roster_timeline.intervals)
    discovered.update(matchup.roster_id for matchup in inputs.archive.matchup_weeks)
    selected = discovered if requested is None else discovered.intersection(requested)
    return tuple(sorted(roster_id for roster_id in selected if roster_id > 0))


def _roster_players_for_week(
    timeline: RosterTimeline,
    roster_id: int,
    boundary: FantasyWeekBoundary,
) -> tuple[str, ...]:
    return timeline.players_overlapping(roster_id, boundary.utc_start, boundary.utc_end)


def _observed_starters(
    matchups: Sequence[HistoricalMatchup],
    week: int,
    roster_id: int,
) -> tuple[str | None, ...]:
    matching = tuple(
        matchup for matchup in matchups if matchup.week == week and matchup.roster_id == roster_id
    )
    if not matching:
        return ()
    return matching[0].starter_ids


def _starter_slots(roster_slots: Sequence[str]) -> tuple[str, ...]:
    reserve_slots = frozenset({"BN", "IR", "IR+", "RES", "TAXI"})
    return tuple(
        slot.strip().upper()
        for slot in roster_slots
        if slot.strip() and slot.strip().upper() not in reserve_slots
    )


def _assembly_quality(
    coverage: ReplayCoverageSummary,
    exclusions: Sequence[ReplayInputExclusion],
) -> PlanningQuality:
    if exclusions:
        return PlanningQuality.PARTIAL
    if coverage.complete:
        return PlanningQuality.EXACT
    if coverage.best_known_eligibility:
        return PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    return PlanningQuality.PARTIAL


def _unique_exclusions(
    exclusions: Sequence[ReplayInputExclusion],
) -> tuple[ReplayInputExclusion, ...]:
    return tuple(dict.fromkeys(exclusions))


__all__ = ("assemble_historical_team_week_inputs",)
