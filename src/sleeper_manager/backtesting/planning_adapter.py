from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from sleeper_manager.backtesting.replay import ReplayConfig
from sleeper_manager.backtesting.replay_models import (
    LockedSlot,
    ReplayGame,
    ReplayPlayerGame,
)
from sleeper_manager.backtesting.replay_state import ReplayState
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.planning import (
    FixedSlot,
    FreshnessSummary,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
    PlanningStateError,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionSnapshot


class PlanningAdapterError(ValueError):
    pass


def team_week_state_from_replay(
    replay_state: ReplayState,
    *,
    config: ReplayConfig,
    decision_time: datetime,
    league_profile: LeagueProfile | None = None,
    observed_starter_ids: Iterable[str] = (),
    roster_player_ids: Iterable[str] | None = None,
    player_positions_by_id: Mapping[str, Iterable[str]] | None = None,
    manager_policy_version: str = "replay-policy-v1",
    input_version: str = "replay-inputs-v1",
) -> TeamWeekState:
    if decision_time.tzinfo is None:
        raise PlanningAdapterError("Replay planning decisions require timezone-aware timestamps")
    starter_slots, replay_slot_to_domain, blocking_reasons = _starter_slots(
        replay_state, league_profile
    )
    games, game_issues = _index_games(replay_state.games)
    blocking_reasons.extend(game_issues)

    player_games = tuple(
        player_game
        for player_game in replay_state.player_games
        if player_game.fantasy_team_id == config.roster_id
    )
    positions_by_player = _player_positions(player_games, player_positions_by_id)
    roster_ids = _roster_player_ids(
        config.roster_id,
        player_games,
        league_profile,
        roster_player_ids,
        blocking_reasons,
    )
    observed_ids = _observed_starter_ids(
        config.roster_id,
        league_profile,
        observed_starter_ids,
    )
    observed = tuple(
        ObservedStarter(
            slot_index=starter_slots[index].index,
            player_id=player_id,
            eligible_positions=positions_by_player.get(player_id, ()),
        )
        for index, player_id in enumerate(observed_ids)
        if index < len(starter_slots)
    )
    if len(observed_ids) > len(starter_slots):
        blocking_reasons.append(PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED)
    if any(not starter.eligible_positions for starter in observed):
        blocking_reasons.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)

    opportunities: list[GameOpportunity] = []
    projection_versions: set[str] = set()
    for player_game in player_games:
        game = games.get(player_game.game_id)
        if game is None:
            blocking_reasons.append(PlanningReasonCode.MISSING_GAME_SCHEDULE)
            continue
        projection, projection_reason = _point_in_time_projection(
            player_game,
            decision_time,
            blocking_reasons,
        )
        if projection is not None:
            projection_versions.add(projection.model_version)
        provider_player_id = player_game.provider_player_id.strip() or None
        if provider_player_id is None:
            blocking_reasons.append(PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY)
        positions = tuple(
            sorted(
                {
                    position.strip().upper()
                    for position in player_game.eligible_positions
                    if position.strip()
                }
            )
        )
        if not positions:
            blocking_reasons.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)
        eligible_slot_indices = tuple(
            slot.index for slot in starter_slots if eligible_for_slot(positions, slot.position)
        )
        finalized_at = _finalized_at(game, decision_time)
        completed_score = (
            player_game.actual_score
            if finalized_at is not None and finalized_at <= decision_time
            else None
        )
        opportunities.append(
            GameOpportunity(
                sleeper_player_id=player_game.sleeper_id,
                provider_player_id=provider_player_id,
                game_id=player_game.game_id,
                scheduled_start=game.start_time,
                status=_planning_status_at(game, decision_time),
                roster_id=config.roster_id,
                membership_segment=player_game.membership_segment,
                eligible_slot_indices=eligible_slot_indices,
                eligible_positions=positions,
                rostered_at_tipoff=player_game.rostered_at_tipoff,
                availability_status="unknown",
                availability_evidence_at=None,
                projection=projection,
                missing_projection_reason=projection_reason,
                completed_fantasy_score=completed_score,
                finalized_at=finalized_at,
                source_lineage=(
                    SourceLineage("replay", input_version, decision_time, decision_time),
                ),
            )
        )

    fixed_slots = tuple(
        _fixed_slot(locked, replay_slot_to_domain, starter_slots, replay_state)
        for locked in replay_state.locked_slots
    )
    passed_opportunities = tuple(
        PassedOpportunity(
            player_id=decision.player_id,
            game_id=decision.game_id,
            decision_time=decision.decision_time,
            decision_id=f"replay-decision-{index}",
            provenance=decision.information_version,
        )
        for index, decision in enumerate(replay_state.decisions)
        if decision.kind == "pass" and decision.game_id is not None
    )
    quality = _planning_quality(config.eligibility_quality)
    projection_model_version = _combined_version(projection_versions, "unknown")
    profile_scoring_version = (
        league_profile.scoring.version if league_profile is not None else "replay-scoring-unknown"
    )
    profile_configuration_version = (
        league_profile.configuration_fingerprint
        if league_profile is not None
        else "replay-configuration-unknown"
    )
    reasons = _unique_reasons(blocking_reasons)
    try:
        return TeamWeekState(
            league_id=config.league_id,
            season=(league_profile.season if league_profile is not None else "unknown"),
            week=config.week,
            roster_id=config.roster_id,
            decision_time=decision_time,
            starter_slots=starter_slots,
            roster_player_ids=roster_ids,
            observed_starters=observed,
            opportunities=tuple(opportunities),
            fixed_slots=fixed_slots,
            passed_opportunities=passed_opportunities,
            scoring_policy_version=profile_scoring_version,
            league_configuration_version=profile_configuration_version,
            manager_policy_version=manager_policy_version,
            projection_model_version=projection_model_version,
            input_version=input_version,
            freshness=FreshnessSummary(
                (SourceLineage("replay", input_version, decision_time, decision_time),)
            ),
            eligibility_quality=quality,
            exclusions=reasons,
            blocking_reasons=reasons,
        )
    except PlanningStateError as error:
        raise PlanningAdapterError(str(error)) from error


def _starter_slots(
    replay_state: ReplayState,
    league_profile: LeagueProfile | None,
) -> tuple[tuple[StarterSlot, ...], dict[int, int], list[PlanningReasonCode]]:
    replay_positions = tuple(position.strip().upper() for position in replay_state.starter_slots)
    blocking: list[PlanningReasonCode] = []
    discovered = (
        tuple(slot for slot in league_profile.roster_slots if slot.is_starting)
        if league_profile is not None
        else ()
    )
    if discovered and tuple(slot.position for slot in discovered) != replay_positions:
        blocking.append(PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED)
    if discovered and len(discovered) == len(replay_positions):
        slots = tuple(StarterSlot(slot.index, slot.position) for slot in discovered)
    else:
        slots = tuple(
            StarterSlot(index, position) for index, position in enumerate(replay_positions)
        )
    return slots, {index: slot.index for index, slot in enumerate(slots)}, blocking


def _index_games(
    games: Iterable[ReplayGame],
) -> tuple[dict[str, ReplayGame], list[PlanningReasonCode]]:
    indexed: dict[str, ReplayGame] = {}
    blocking: list[PlanningReasonCode] = []
    for game in games:
        previous = indexed.get(game.game_id)
        if previous is not None:
            blocking.append(PlanningReasonCode.AMBIGUOUS_EVENT_ORDER)
            continue
        indexed[game.game_id] = game
    return indexed, blocking


def _player_positions(
    player_games: Iterable[ReplayPlayerGame],
    supplied: Mapping[str, Iterable[str]] | None,
) -> dict[str, tuple[str, ...]]:
    positions: dict[str, set[str]] = {}
    for player_game in player_games:
        positions.setdefault(player_game.sleeper_id, set()).update(
            position.strip().upper()
            for position in player_game.eligible_positions
            if position.strip()
        )
    if supplied is not None:
        for player_id, player_positions in supplied.items():
            positions[player_id] = {
                position.strip().upper() for position in player_positions if position.strip()
            }
    return {
        player_id: tuple(sorted(player_positions))
        for player_id, player_positions in positions.items()
    }


def _roster_player_ids(
    roster_id: int,
    player_games: Iterable[ReplayPlayerGame],
    league_profile: LeagueProfile | None,
    supplied: Iterable[str] | None,
    blocking: list[PlanningReasonCode],
) -> tuple[str, ...]:
    if supplied is not None:
        return _clean_ids(supplied)
    if league_profile is not None:
        roster = next(
            (roster for roster in league_profile.rosters if roster.roster_id == roster_id), None
        )
        if roster is not None:
            return _clean_ids(roster.player_ids)
        blocking.append(PlanningReasonCode.MISSING_ROSTER_SNAPSHOT)
    return tuple(sorted({player_game.sleeper_id for player_game in player_games}))


def _observed_starter_ids(
    roster_id: int,
    league_profile: LeagueProfile | None,
    supplied: Iterable[str],
) -> tuple[str, ...]:
    supplied_ids = _clean_ids(supplied)
    if supplied_ids:
        return supplied_ids
    if league_profile is None:
        return ()
    roster = next(
        (roster for roster in league_profile.rosters if roster.roster_id == roster_id), None
    )
    return _clean_ids(roster.starter_ids) if roster is not None else ()


def _point_in_time_projection(
    player_game: ReplayPlayerGame,
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
) -> tuple[ProjectionSnapshot | None, PlanningReasonCode | None]:
    projection = player_game.projection
    if projection is None:
        blocking.append(PlanningReasonCode.MISSING_PROJECTION)
        return None, PlanningReasonCode.MISSING_PROJECTION
    if projection.available_as_of.tzinfo is None:
        raise PlanningAdapterError("Projection availability must be timezone-aware")
    if projection.available_as_of > decision_time:
        blocking.append(PlanningReasonCode.PROJECTION_AFTER_DECISION)
        return None, PlanningReasonCode.PROJECTION_AFTER_DECISION
    return projection, None


def _fixed_slot(
    locked: LockedSlot,
    replay_slot_to_domain: Mapping[int, int],
    starter_slots: tuple[StarterSlot, ...],
    replay_state: ReplayState,
) -> FixedSlot:
    slot_index = replay_slot_to_domain.get(locked.slot_index)
    if slot_index is None:
        raise PlanningAdapterError("Locked slot index is outside the replay starter slots")
    slot = next(slot for slot in starter_slots if slot.index == slot_index)
    decision = next(
        (
            decision
            for decision in replay_state.decisions
            if decision.kind == "lock"
            and decision.player_id == locked.sleeper_id
            and decision.game_id == locked.game_id
            and decision.slot_index == locked.slot_index
        ),
        None,
    )
    return FixedSlot(
        slot_index=slot_index,
        slot_position=slot.position,
        player_id=locked.sleeper_id,
        game_id=locked.game_id,
        accepted_fantasy_score=locked.score,
        decision_time=locked.locked_at,
        decision_id=(f"replay-lock-{locked.sleeper_id}-{locked.game_id}-{locked.slot_index}"),
        provenance=decision.information_version if decision is not None else "replay",
    )


def _finalized_at(game: ReplayGame, decision_time: datetime) -> datetime | None:
    finalized_at = game.finalized_at
    if finalized_at is None or finalized_at > decision_time:
        return None
    return finalized_at


def _planning_status_at(game: ReplayGame, decision_time: datetime) -> PlanningGameStatus:
    finalized_at = game.finalized_at
    if finalized_at is not None and finalized_at > decision_time:
        return (
            PlanningGameStatus.ACTIVE
            if game.start_time <= decision_time
            else PlanningGameStatus.SCHEDULED
        )
    return PlanningGameStatus(game.status.value)


def _planning_quality(value: str) -> PlanningQuality:
    try:
        return PlanningQuality(value)
    except ValueError:
        return PlanningQuality.UNKNOWN


def _combined_version(versions: set[str], default: str) -> str:
    if not versions:
        return default
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed:" + ",".join(sorted(versions))


def _unique_reasons(
    reasons: Iterable[PlanningReasonCode],
) -> tuple[PlanningReasonCode, ...]:
    return tuple(dict.fromkeys(reasons))


def _clean_ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(value.strip() for value in values if value.strip() and value.strip() != "0")
    )


__all__ = ("PlanningAdapterError", "team_week_state_from_replay")
