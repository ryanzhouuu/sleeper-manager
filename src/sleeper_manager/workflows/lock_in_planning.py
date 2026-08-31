"""Create live opportunities and coalesced postgame watches from planning evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.scoring import calculate_fantasy_points
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    DueWorkKind,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInObservation,
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.lock_in_evidence import merge_lock_in_opportunity_evidence
from sleeper_manager.workflows.planning_inputs import (
    LivePlanningInputs,
    PlayerEligibilityEvidence,
    ResolvedPlayerIdentity,
)
from sleeper_manager.workflows.postgame_lock_in import (
    DirectGameSummarySource,
    summary_fingerprint,
    summary_wait_reason,
)

_POSTGAME_INITIAL_DELAY = timedelta(hours=2)


async def sync_live_lock_in_opportunities(
    inputs: LivePlanningInputs,
    *,
    repository: AsyncRuntimeStateRepository,
    observed_at: datetime,
) -> None:
    """Persist current roster schedule evidence and one postgame row per NBA game."""

    profile = inputs.league_profile
    roster = next(item for item in profile.rosters if item.roster_id == profile.manager_roster_id)
    identities = {item.sleeper_player_id: item for item in inputs.identities}
    eligibility = _eligibility_by_player(inputs.player_eligibility)
    games = _in_window_games(inputs)
    games_by_team = _games_by_team(games)
    starters = _starter_evidence(inputs, roster.starter_ids, eligibility)
    watched_games: dict[str, tuple[ScheduledGame, datetime]] = {}
    for player_id in roster.player_ids:
        identity = identities.get(player_id)
        if (
            identity is None
            or identity.provider_player_id is None
            or identity.provider_team_id is None
        ):
            continue
        player_games = tuple(games_by_team.get(identity.provider_team_id, ()))
        for index, game in enumerate(player_games):
            deadline = _action_deadline(inputs, player_games, index)
            candidate = _opportunity_record(
                inputs,
                player_id=player_id,
                identity=identity,
                game=game,
                deadline=deadline,
                starter=starters.get(player_id),
                listed_as_starter=player_id in roster.starter_ids,
                observed_at=observed_at,
            )
            await _guarded_upsert(repository, candidate)
            if candidate.status is LockInOpportunityStatus.INELIGIBLE:
                continue
            previous = watched_games.get(game.provider_id)
            watch_deadline = max(deadline, previous[1]) if previous else deadline
            watched_games[game.provider_id] = (game, watch_deadline)
    for game, deadline in watched_games.values():
        await repository.upsert_scheduled_work(
            _postgame_work(game, deadline=deadline, observed_at=observed_at)
        )


def _eligibility_by_player(
    evidence: tuple[PlayerEligibilityEvidence, ...],
) -> dict[str, PlayerEligibilityEvidence]:
    """Retain only unambiguous latest eligibility evidence per player."""

    result: dict[str, PlayerEligibilityEvidence] = {}
    conflicts: set[str] = set()
    for item in sorted(evidence, key=lambda value: value.available_as_of):
        previous = result.get(item.sleeper_player_id)
        if previous is not None and previous.eligible_positions != item.eligible_positions:
            conflicts.add(item.sleeper_player_id)
            continue
        result[item.sleeper_player_id] = item
    return {key: value for key, value in result.items() if key not in conflicts}


def _in_window_games(inputs: LivePlanningInputs) -> tuple[ScheduledGame, ...]:
    """Deduplicate consistent games and exclude records outside the fantasy week."""

    indexed: dict[str, ScheduledGame] = {}
    conflicts: set[str] = set()
    for result in inputs.schedule_results:
        for game in result.games:
            if not inputs.week_window.contains(game.start_time):
                continue
            previous = indexed.get(game.provider_id)
            if previous is not None and _game_facts(previous) != _game_facts(game):
                conflicts.add(game.provider_id)
                continue
            indexed[game.provider_id] = game
    return tuple(
        sorted(
            (game for key, game in indexed.items() if key not in conflicts),
            key=lambda game: (game.start_time, game.provider_id),
        )
    )


def _game_facts(game: ScheduledGame) -> tuple[object, ...]:
    """Return schedule facts whose conflict would make an opportunity unsafe."""

    return (
        game.start_time,
        game.status,
        game.home_team_id,
        game.away_team_id,
        game.finalized_at,
    )


def _games_by_team(games: tuple[ScheduledGame, ...]) -> dict[str, tuple[ScheduledGame, ...]]:
    """Index each game under both participating provider teams."""

    indexed: dict[str, list[ScheduledGame]] = {}
    for game in games:
        indexed.setdefault(game.home_team_id, []).append(game)
        indexed.setdefault(game.away_team_id, []).append(game)
    return {team: tuple(values) for team, values in indexed.items()}


def _starter_evidence(
    inputs: LivePlanningInputs,
    starter_ids: tuple[str | None, ...],
    eligibility: dict[str, PlayerEligibilityEvidence],
) -> dict[str, tuple[int, str, tuple[str, ...], datetime] | None]:
    """Map the current Sleeper starter sequence to exact configured slots."""

    slots = tuple(
        sorted(
            (slot for slot in inputs.league_profile.roster_slots if slot.is_starting),
            key=lambda slot: slot.index,
        )
    )
    result: dict[str, tuple[int, str, tuple[str, ...], datetime] | None] = {}
    for sequence_index, player_id in enumerate(starter_ids):
        if player_id is None or sequence_index >= len(slots):
            continue
        item = eligibility.get(player_id)
        slot = slots[sequence_index]
        if item is None or not eligible_for_slot(item.eligible_positions, slot.position):
            result[player_id] = None
            continue
        result[player_id] = (
            slot.index,
            slot.position,
            item.eligible_positions,
            inputs.league_profile.retrieved_at,
        )
    return result


def _action_deadline(
    inputs: LivePlanningInputs,
    games: tuple[ScheduledGame, ...],
    index: int,
) -> datetime:
    """Use the next player-game lead-time boundary capped by fantasy-week end."""

    if index + 1 >= len(games):
        return inputs.week_window.ends_at
    return min(
        games[index + 1].start_time - inputs.move_lead_time,
        inputs.week_window.ends_at,
    )


def _opportunity_record(
    inputs: LivePlanningInputs,
    *,
    player_id: str,
    identity: ResolvedPlayerIdentity,
    game: ScheduledGame,
    deadline: datetime,
    starter: tuple[int, str, tuple[str, ...], datetime] | None,
    listed_as_starter: bool,
    observed_at: datetime,
) -> LockInOpportunityRecord:
    """Build current schedule state and safe pre-tipoff evidence when available."""

    snapshot_at = inputs.league_profile.retrieved_at
    evidence_precedes_tipoff = snapshot_at <= game.start_time
    slot_index, slot_position, positions, evidence_at = (
        starter if evidence_precedes_tipoff and starter is not None else (None, None, (), None)
    )
    if evidence_precedes_tipoff:
        evidence_at = snapshot_at
    rostered = listed_as_starter if evidence_precedes_tipoff else None
    status = _initial_status(game)
    if evidence_precedes_tipoff and starter is None:
        status = LockInOpportunityStatus.INELIGIBLE
    return LockInOpportunityRecord(
        key=LockInOpportunityKey(
            inputs.league_profile.league_id,
            inputs.week_window.week,
            inputs.league_profile.manager_roster_id,
            player_id,
            game.provider_id,
        ),
        provider_player_id=identity.provider_player_id or "",
        scheduled_start=game.start_time,
        action_deadline=deadline,
        fantasy_week_end=inputs.week_window.ends_at,
        status=status,
        next_check_at=game.start_time + _POSTGAME_INITIAL_DELAY,
        created_at=observed_at,
        updated_at=observed_at,
        slot_index=slot_index,
        slot_position=slot_position,
        eligible_positions=positions,
        rostered_at_tipoff=rostered,
        roster_evidence_at=evidence_at,
        league_configuration_fingerprint=inputs.league_profile.configuration_fingerprint,
    )


def _initial_status(game: ScheduledGame) -> LockInOpportunityStatus:
    """Translate provider schedule status without treating a final as stable."""

    if game.status is GameStatus.IN_PROGRESS:
        return LockInOpportunityStatus.ACTIVE
    if game.status is GameStatus.FINAL:
        return LockInOpportunityStatus.FINALIZING
    if game.status in (GameStatus.POSTPONED, GameStatus.CANCELED, GameStatus.UNKNOWN):
        return LockInOpportunityStatus.RECONCILIATION_REQUIRED
    return LockInOpportunityStatus.SCHEDULED


async def _guarded_upsert(
    repository: AsyncRuntimeStateRepository,
    candidate: LockInOpportunityRecord,
) -> None:
    """Insert or merge schedule/evidence without erasing live observations."""

    if await repository.upsert_lock_in_opportunity(candidate):
        return
    current = await repository.get_lock_in_opportunity(candidate.key)
    if current is None:
        return
    merged = _merge_planning_evidence(current, candidate)
    await repository.update_lock_in_opportunity(merged, expected_version=current.row_version)


def _merge_planning_evidence(
    current: LockInOpportunityRecord,
    candidate: LockInOpportunityRecord,
) -> LockInOpportunityRecord:
    """Refresh schedule and newer pre-tipoff evidence while retaining lifecycle progress."""

    advanced = current.status not in {
        LockInOpportunityStatus.SCHEDULED,
        LockInOpportunityStatus.ACTIVE,
        LockInOpportunityStatus.INELIGIBLE,
        LockInOpportunityStatus.RECONCILIATION_REQUIRED,
    }
    newer_evidence = candidate.roster_evidence_at is not None and (
        current.roster_evidence_at is None
        or candidate.roster_evidence_at > current.roster_evidence_at
    )
    league_changed = (
        current.league_configuration_fingerprint is not None
        and current.league_configuration_fingerprint != candidate.league_configuration_fingerprint
    )
    return replace(
        current,
        provider_player_id=candidate.provider_player_id,
        scheduled_start=candidate.scheduled_start,
        action_deadline=candidate.action_deadline,
        fantasy_week_end=candidate.fantasy_week_end,
        status=(
            LockInOpportunityStatus.RECONCILIATION_REQUIRED
            if league_changed
            else current.status
            if current.status is LockInOpportunityStatus.INELIGIBLE
            and candidate.roster_evidence_at is None
            else current.status
            if advanced
            else candidate.status
        ),
        next_check_at=(current.next_check_at if advanced else candidate.next_check_at),
        slot_index=candidate.slot_index if newer_evidence else current.slot_index,
        slot_position=candidate.slot_position if newer_evidence else current.slot_position,
        eligible_positions=(
            candidate.eligible_positions if newer_evidence else current.eligible_positions
        ),
        rostered_at_tipoff=(
            candidate.rostered_at_tipoff if newer_evidence else current.rostered_at_tipoff
        ),
        roster_evidence_at=(
            candidate.roster_evidence_at if newer_evidence else current.roster_evidence_at
        ),
        league_configuration_fingerprint=candidate.league_configuration_fingerprint,
        updated_at=candidate.updated_at,
    )


def _postgame_work(
    game: ScheduledGame,
    *,
    deadline: datetime,
    observed_at: datetime,
) -> ScheduledWorkRecord:
    """Build the single game-level watch shared by every relevant roster player."""

    key = f"postgame:{game.provider_id}"
    return ScheduledWorkRecord(
        work_id=sha256(key.encode()).hexdigest()[:32],
        dedupe_key=key,
        kind=DueWorkKind.POSTGAME,
        due_at=game.start_time + _POSTGAME_INITIAL_DELAY,
        status=ScheduledWorkStatus.PENDING,
        game_id=game.provider_id,
        deadline=deadline,
        created_at=observed_at,
        updated_at=observed_at,
    )


async def refresh_terminal_lock_in_scores(
    inputs: LivePlanningInputs,
    *,
    repository: AsyncRuntimeStateRepository,
    fetch_summary: DirectGameSummarySource,
    observed_at: datetime,
    poll_id: str,
) -> LivePlanningInputs:
    """Re-observe terminal scores and return inputs containing any stabilized correction."""

    if observed_at > inputs.week_window.ends_at:
        return inputs
    records = await repository.load_acknowledged_lock_in_opportunities(
        inputs.league_profile.league_id,
        inputs.week_window.week,
    )
    if not records:
        return inputs
    grouped: dict[str, list[LockInOpportunityRecord]] = {}
    for record in records:
        grouped.setdefault(record.key.game_id, []).append(record)
    for game_id, group in grouped.items():
        opportunities = tuple(group)
        summary_result = await fetch_summary(game_id)
        if summary_wait_reason(summary_result, opportunities) is not None:
            continue
        fingerprint = summary_fingerprint(summary_result.records)
        boxes = {item.player_id: item for item in summary_result.records.player_box_scores}
        for opportunity in opportunities:
            box_score = boxes.get(opportunity.provider_player_id)
            if box_score is None:
                continue
            score = calculate_fantasy_points(box_score.line, inputs.league_profile.scoring)
            await repository.record_lock_in_observation(
                opportunity.key,
                LockInObservation(
                    score=score,
                    fingerprint=fingerprint,
                    poll_id=f"{poll_id}:{game_id}",
                    observed_at=observed_at,
                    next_check_at=opportunity.next_check_at,
                ),
                expected_version=opportunity.row_version,
            )
    refreshed = await repository.load_acknowledged_lock_in_opportunities(
        inputs.league_profile.league_id,
        inputs.week_window.week,
    )
    return replace(
        inputs,
        acknowledgements=merge_lock_in_opportunity_evidence(inputs.acknowledgements, refreshed),
    )


__all__ = ["refresh_terminal_lock_in_scores", "sync_live_lock_in_opportunities"]
