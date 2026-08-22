"""Assemble the shared point-in-time team-week state from live provider evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    PlayerAvailability,
    ScheduledGame,
)
from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
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


class PlanningInputsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FantasyWeekWindow:
    """Explicit timezone-aware fantasy-week boundaries used to scope games."""

    week: int
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.week <= 0:
            raise PlanningInputsError("Fantasy weeks must be positive")
        _require_aware(self.starts_at, "Fantasy-week start")
        _require_aware(self.ends_at, "Fantasy-week end")
        if self.starts_at >= self.ends_at:
            raise PlanningInputsError("Fantasy-week end must follow its start")

    def contains(self, at: datetime) -> bool:
        return self.starts_at <= at < self.ends_at


@dataclass(frozen=True, slots=True)
class PlayerEligibilityEvidence:
    sleeper_player_id: str
    eligible_positions: tuple[str, ...]
    available_as_of: datetime
    provenance: str

    def __post_init__(self) -> None:
        _require_text(self.sleeper_player_id, "Eligibility player ID")
        _require_aware(self.available_as_of, "Eligibility availability")
        _require_text(self.provenance, "Eligibility provenance")
        object.__setattr__(
            self,
            "eligible_positions",
            tuple(sorted({position.strip().upper() for position in self.eligible_positions})),
        )


@dataclass(frozen=True, slots=True)
class ResolvedPlayerIdentity:
    sleeper_player_id: str
    provider_player_id: str | None
    provider_team_id: str | None
    method: str
    confidence: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.sleeper_player_id, "Identity player ID")
        if self.provider_player_id is not None:
            _require_text(self.provider_player_id, "Provider player ID")
        if self.provider_team_id is not None:
            _require_text(self.provider_team_id, "Provider team ID")
        _require_text(self.method, "Identity method")
        _require_text(self.confidence, "Identity confidence")
        _require_text(self.reason, "Identity reason")


@dataclass(frozen=True, slots=True)
class ScheduleResourceResult:
    resource: str
    games: tuple[ScheduledGame, ...]
    quality: DataQualityReport

    def __post_init__(self) -> None:
        _require_text(self.resource, "Schedule resource")


@dataclass(frozen=True, slots=True)
class AvailabilityResourceResult:
    resource: str
    records: tuple[PlayerAvailability, ...]
    quality: DataQualityReport

    def __post_init__(self) -> None:
        _require_text(self.resource, "Availability resource")


@dataclass(frozen=True, slots=True)
class LiveProjectionResult:
    sleeper_player_id: str
    game_id: str
    snapshot: ProjectionSnapshot | None
    missing_reason: PlanningReasonCode | None

    def __post_init__(self) -> None:
        _require_text(self.sleeper_player_id, "Projection player ID")
        _require_text(self.game_id, "Projection game ID")
        if self.snapshot is None and self.missing_reason is None:
            raise PlanningInputsError("Missing projections require an explicit reason")
        if self.snapshot is not None and self.missing_reason is not None:
            raise PlanningInputsError("Projection and missing-projection reason are exclusive")


@dataclass(frozen=True, slots=True)
class PlanningFreshnessPolicy:
    """Injected staleness tolerances; no operational defaults are assumed."""

    max_sleeper_age: timedelta
    max_nba_schedule_age: timedelta
    max_availability_age: timedelta

    def __post_init__(self) -> None:
        for name, value in (
            ("Sleeper age limit", self.max_sleeper_age),
            ("NBA schedule age limit", self.max_nba_schedule_age),
            ("Availability age limit", self.max_availability_age),
        ):
            if value <= timedelta(0):
                raise PlanningInputsError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class LivePlanningInputs:
    league_profile: LeagueProfile
    week_window: FantasyWeekWindow
    freshness_policy: PlanningFreshnessPolicy
    player_eligibility: tuple[PlayerEligibilityEvidence, ...] = ()
    identities: tuple[ResolvedPlayerIdentity, ...] = ()
    schedule_results: tuple[ScheduleResourceResult, ...] = ()
    availability_results: tuple[AvailabilityResourceResult, ...] = ()
    projections: tuple[LiveProjectionResult, ...] = ()
    acknowledgements: tuple[AcknowledgedDecisionEvidence, ...] = ()
    identity_quality_reports: tuple[DataQualityReport, ...] = ()

    def __post_init__(self) -> None:
        if self.week_window.week != self.league_profile.fantasy_week.week:
            raise PlanningInputsError("Fantasy-week window does not match the league profile")


def build_live_team_week_state(
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
) -> TeamWeekState:
    """Produce the shared validated state without network access or planner calls."""
    _require_aware(decision_time, "Decision time")
    profile = inputs.league_profile
    blocking: list[PlanningReasonCode] = []
    warnings: list[PlanningReasonCode] = []

    _apply_freshness(inputs, decision_time, blocking, warnings)
    roster = _manager_roster(profile, blocking)
    starter_slots = _starter_slots(profile, blocking)
    positions_by_player = _positions_by_player(
        inputs.player_eligibility, inputs.freshness_policy, decision_time, blocking
    )
    identity_by_player = _identities_by_player(inputs.identities, blocking)
    games, game_provenance = _in_window_games(inputs.schedule_results, inputs.week_window, blocking)
    availability_by_provider = _availability_by_provider(inputs.availability_results, decision_time)
    projections_by_key = _projections_by_key(inputs.projections)

    roster_player_ids = roster.player_ids if roster is not None else ()
    observed = (
        _observed_starters(profile, roster, starter_slots, positions_by_player, blocking)
        if roster is not None
        else ()
    )

    opportunities: list[GameOpportunity] = []
    projection_versions: set[str] = set()
    opportunity_keys: set[tuple[str, str]] = set()
    for player_id in roster_player_ids:
        identity = identity_by_player.get(player_id)
        positions = positions_by_player.get(player_id, ())
        if not positions:
            blocking.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)
            continue
        if identity is None:
            blocking.append(PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY)
            continue
        eligible_slot_indices = _eligible_slot_indices(positions, starter_slots)
        player_games = _player_games(identity, games, blocking)
        for game in player_games:
            key = player_id, game.provider_id
            if key in opportunity_keys:
                continue
            opportunity_keys.add(key)
            snapshot, missing_reason = _projection_for(
                projections_by_key.get(key), decision_time, blocking
            )
            if snapshot is not None:
                projection_versions.add(snapshot.model_version)
            availability_status, availability_evidence_at = _availability_for(
                identity.provider_player_id or "", availability_by_provider
            )
            opportunities.append(
                GameOpportunity(
                    sleeper_player_id=player_id,
                    provider_player_id=identity.provider_player_id,
                    game_id=game.provider_id,
                    scheduled_start=game.start_time,
                    status=_planning_status(game),
                    roster_id=profile.manager_roster_id,
                    membership_segment=None,
                    eligible_slot_indices=eligible_slot_indices,
                    eligible_positions=positions,
                    rostered_at_tipoff=True,
                    availability_status=availability_status,
                    availability_evidence_at=availability_evidence_at,
                    projection=snapshot,
                    missing_projection_reason=missing_reason,
                    completed_fantasy_score=None,
                    finalized_at=_finalized_at(game, decision_time),
                    source_lineage=_opportunity_lineage(
                        game, game_provenance.get(game.provider_id, ())
                    ),
                    data_quality="live-current-roster",
                )
            )

    fixed_slots, passed_opportunities = _acknowledgement_constraints(
        inputs.acknowledgements,
        opportunity_keys,
        positions_by_player,
        starter_slots,
        decision_time,
        blocking,
    )

    reasons = _unique_reasons(blocking)
    warning_reasons = tuple(reason for reason in _unique_reasons(warnings) if reason not in reasons)
    try:
        return TeamWeekState(
            league_id=profile.league_id,
            season=profile.season,
            week=inputs.week_window.week,
            roster_id=profile.manager_roster_id,
            decision_time=decision_time,
            starter_slots=starter_slots,
            roster_player_ids=tuple(dict.fromkeys(roster_player_ids)),
            observed_starters=observed,
            opportunities=tuple(opportunities),
            fixed_slots=fixed_slots,
            passed_opportunities=passed_opportunities,
            scoring_policy_version=profile.scoring.version,
            league_configuration_version=profile.configuration_fingerprint,
            manager_policy_version=f"live-policy:{profile.mode.value}",
            projection_model_version=_combined_version(projection_versions),
            input_version="live-inputs-v1",
            freshness=_freshness_summary(inputs),
            eligibility_quality=(
                PlanningQuality.PARTIAL
                if reasons or _missing_eligibility(roster_player_ids, positions_by_player)
                else PlanningQuality.EXACT
            ),
            exclusions=reasons,
            warnings=warning_reasons,
            blocking_reasons=reasons,
        )
    except PlanningStateError as error:
        raise PlanningInputsError(str(error)) from error


def _apply_freshness(
    inputs: LivePlanningInputs,
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
    warnings: list[PlanningReasonCode],
) -> None:
    policy = inputs.freshness_policy
    if decision_time - inputs.league_profile.retrieved_at > policy.max_sleeper_age:
        blocking.append(PlanningReasonCode.STALE_SLEEPER_STATE)
    for schedule_result in inputs.schedule_results:
        _apply_resource_freshness(
            schedule_result.quality,
            policy.max_nba_schedule_age,
            decision_time,
            blocking,
            warnings,
        )
    for availability_result in inputs.availability_results:
        _apply_resource_freshness(
            availability_result.quality,
            policy.max_availability_age,
            decision_time,
            blocking,
            warnings,
        )
    for report in inputs.identity_quality_reports:
        _apply_resource_freshness(
            report,
            policy.max_nba_schedule_age,
            decision_time,
            blocking,
            warnings,
        )


def _apply_resource_freshness(
    quality: DataQualityReport,
    max_age: timedelta,
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
    warnings: list[PlanningReasonCode],
) -> None:
    if quality.state is DataQualityState.ERROR:
        blocking.append(PlanningReasonCode.STALE_NBA_STATE)
        return
    if decision_time - quality.retrieved_at > max_age:
        blocking.append(PlanningReasonCode.STALE_NBA_STATE)
        return
    if (
        quality.state is DataQualityState.STALE
        or quality.state is DataQualityState.PARTIAL
        or quality.state is DataQualityState.UNRESOLVED
    ):
        warnings.append(PlanningReasonCode.STALE_NBA_STATE)


def _manager_roster(profile: LeagueProfile, blocking: list[PlanningReasonCode]) -> Roster | None:
    roster = next(
        (item for item in profile.rosters if item.roster_id == profile.manager_roster_id),
        None,
    )
    if roster is None:
        blocking.append(PlanningReasonCode.MISSING_ROSTER_SNAPSHOT)
    return roster


def _starter_slots(
    profile: LeagueProfile, blocking: list[PlanningReasonCode]
) -> tuple[StarterSlot, ...]:
    slots = tuple(
        StarterSlot(slot.index, slot.position)
        for slot in sorted(
            (slot for slot in profile.roster_slots if slot.is_starting),
            key=lambda slot: slot.index,
        )
    )
    if not slots:
        blocking.append(PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED)
    return slots


def _positions_by_player(
    evidence: tuple[PlayerEligibilityEvidence, ...],
    policy: PlanningFreshnessPolicy,
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
) -> dict[str, tuple[str, ...]]:
    by_player: dict[str, PlayerEligibilityEvidence] = {}
    for item in evidence:
        if item.available_as_of > decision_time:
            blocking.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)
            continue
        if decision_time - item.available_as_of > policy.max_sleeper_age:
            blocking.append(PlanningReasonCode.STALE_SLEEPER_STATE)
            continue
        previous = by_player.get(item.sleeper_player_id)
        if previous is not None:
            if previous.eligible_positions == item.eligible_positions:
                continue
            blocking.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)
            by_player.pop(item.sleeper_player_id, None)
            continue
        by_player[item.sleeper_player_id] = item
    return {
        player_id: item.eligible_positions
        for player_id, item in by_player.items()
        if item.eligible_positions
    }


def _identities_by_player(
    identities: tuple[ResolvedPlayerIdentity, ...],
    blocking: list[PlanningReasonCode],
) -> dict[str, ResolvedPlayerIdentity]:
    by_player: dict[str, ResolvedPlayerIdentity] = {}
    seen: set[str] = set()
    for identity in identities:
        if identity.sleeper_player_id in seen:
            blocking.append(PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY)
            continue
        seen.add(identity.sleeper_player_id)
        if identity.provider_player_id is None:
            blocking.append(PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY)
            continue
        by_player[identity.sleeper_player_id] = identity
    return by_player


def _in_window_games(
    results: tuple[ScheduleResourceResult, ...],
    window: FantasyWeekWindow,
    blocking: list[PlanningReasonCode],
) -> tuple[tuple[ScheduledGame, ...], dict[str, tuple[str, ...]]]:
    indexed: dict[str, ScheduledGame] = {}
    facts_by_id: dict[str, tuple[object, ...]] = {}
    provenance: dict[str, list[str]] = {}
    for result in results:
        if result.quality.state is DataQualityState.ERROR:
            continue
        for game in result.games:
            facts = _canonical_game_facts(game)
            previous = indexed.get(game.provider_id)
            if previous is not None:
                provenance[game.provider_id].append(result.resource)
                if facts_by_id[game.provider_id] != facts:
                    blocking.append(PlanningReasonCode.AMBIGUOUS_EVENT_ORDER)
                continue
            indexed[game.provider_id] = game
            facts_by_id[game.provider_id] = facts
            provenance[game.provider_id] = [result.resource]
    games = tuple(
        game
        for game in sorted(indexed.values(), key=lambda item: (item.start_time, item.provider_id))
        if window.contains(game.start_time)
    )
    return games, {game.provider_id: tuple(provenance.get(game.provider_id, ())) for game in games}


def _canonical_game_facts(game: ScheduledGame) -> tuple[object, ...]:
    return (
        game.start_time,
        game.status.value,
        game.home_team_id,
        game.away_team_id,
        game.finalized_at,
        game.venue_id,
        game.neutral_site,
    )


def _availability_by_provider(
    results: tuple[AvailabilityResourceResult, ...],
    decision_time: datetime,
) -> dict[str, PlayerAvailability]:
    by_provider: dict[str, PlayerAvailability] = {}
    for result in results:
        if result.quality.state is DataQualityState.ERROR:
            continue
        for record in result.records:
            if record.source.retrieved_at > decision_time:
                continue
            by_provider[record.player_id] = record
    return by_provider


def _projections_by_key(
    projections: tuple[LiveProjectionResult, ...],
) -> dict[tuple[str, str], LiveProjectionResult]:
    return {(item.sleeper_player_id, item.game_id): item for item in projections}


def _observed_starters(
    profile: LeagueProfile,
    roster: Roster,
    starter_slots: tuple[StarterSlot, ...],
    positions_by_player: dict[str, tuple[str, ...]],
    blocking: list[PlanningReasonCode],
) -> tuple[ObservedStarter, ...]:
    starter_ids = _clean_optional_ids(roster.starter_ids)
    if len(starter_ids) > len(starter_slots):
        blocking.append(PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED)
    observed: list[ObservedStarter] = []
    for index, player_id in enumerate(starter_ids):
        if index >= len(starter_slots) or player_id is None:
            continue
        positions = positions_by_player.get(player_id, ())
        slot = starter_slots[index]
        if not positions:
            blocking.append(PlanningReasonCode.AMBIGUOUS_ELIGIBILITY)
            continue
        if not eligible_for_slot(positions, slot.position):
            blocking.append(PlanningReasonCode.LEAGUE_CONFIGURATION_CHANGED)
            continue
        observed.append(ObservedStarter(slot.index, player_id, positions))
    return tuple(observed)


def _player_games(
    identity: ResolvedPlayerIdentity,
    games: tuple[ScheduledGame, ...],
    blocking: list[PlanningReasonCode],
) -> tuple[ScheduledGame, ...]:
    team_id = identity.provider_team_id
    if team_id is None:
        blocking.append(PlanningReasonCode.MISSING_GAME_SCHEDULE)
        return ()
    return tuple(game for game in games if team_id in (game.home_team_id, game.away_team_id))


def _eligible_slot_indices(
    positions: tuple[str, ...], starter_slots: tuple[StarterSlot, ...]
) -> tuple[int, ...]:
    if not positions:
        return ()
    return tuple(
        slot.index for slot in starter_slots if eligible_for_slot(positions, slot.position)
    )


def _projection_for(
    result: LiveProjectionResult | None,
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
) -> tuple[ProjectionSnapshot | None, PlanningReasonCode | None]:
    if result is None or result.snapshot is None:
        reason = (
            PlanningReasonCode.MISSING_PROJECTION
            if result is None
            else result.missing_reason or PlanningReasonCode.MISSING_PROJECTION
        )
        return None, reason
    snapshot = result.snapshot
    if snapshot.available_as_of > decision_time:
        blocking.append(PlanningReasonCode.PROJECTION_AFTER_DECISION)
        return None, PlanningReasonCode.PROJECTION_AFTER_DECISION
    return snapshot, None


def _availability_for(
    provider_player_id: str,
    availability_by_provider: dict[str, PlayerAvailability],
) -> tuple[str, datetime | None]:
    record = availability_by_provider.get(provider_player_id)
    if record is None:
        return "unknown", None
    return record.status.value, record.source.retrieved_at


def _planning_status(game: ScheduledGame) -> PlanningGameStatus:
    return {
        GameStatus.SCHEDULED: PlanningGameStatus.SCHEDULED,
        GameStatus.IN_PROGRESS: PlanningGameStatus.ACTIVE,
        GameStatus.FINAL: PlanningGameStatus.FINAL,
        GameStatus.POSTPONED: PlanningGameStatus.POSTPONED,
        GameStatus.CANCELED: PlanningGameStatus.CANCELED,
        GameStatus.UNKNOWN: PlanningGameStatus.SCHEDULED,
    }[game.status]


def _finalized_at(game: ScheduledGame, decision_time: datetime) -> datetime | None:
    if game.finalized_at is None or game.finalized_at > decision_time:
        return None
    return game.finalized_at


def _opportunity_lineage(
    game: ScheduledGame, reporting_resources: tuple[str, ...]
) -> tuple[SourceLineage, ...]:
    lineages = [
        SourceLineage(
            source=f"nba:{game.source.provider}:{resource}",
            version=game.source.schema_version,
            available_as_of=game.source.retrieved_at,
            retrieved_at=game.source.retrieved_at,
        )
        for resource in reporting_resources
    ]
    if not lineages:
        lineages.append(
            SourceLineage(
                source=f"nba:{game.source.provider}",
                version=game.source.schema_version,
                available_as_of=game.source.retrieved_at,
                retrieved_at=game.source.retrieved_at,
            )
        )
    return tuple(lineages)


def _acknowledgement_constraints(
    acknowledgements: tuple[AcknowledgedDecisionEvidence, ...],
    opportunity_keys: set[tuple[str, str]],
    positions_by_player: Mapping[str, tuple[str, ...]],
    starter_slots: tuple[StarterSlot, ...],
    decision_time: datetime,
    blocking: list[PlanningReasonCode],
) -> tuple[tuple[FixedSlot, ...], tuple[PassedOpportunity, ...]]:
    slot_positions = {slot.index: slot.position for slot in starter_slots}
    fixed: list[FixedSlot] = []
    passed: list[PassedOpportunity] = []
    locked_slots: dict[int, str] = {}
    locked_players: dict[str, str] = {}
    passed_keys: set[tuple[str, str]] = set()
    locked_keys: set[tuple[str, str]] = set()
    for evidence in acknowledgements:
        key = evidence.player_id, evidence.game_id
        conflict = (
            not evidence.reconciled
            or evidence.decided_at > decision_time
            or key not in opportunity_keys
            or _invalid_lock_slot(evidence, slot_positions, positions_by_player)
            or (evidence.action is AcknowledgedAction.LOCK and key in locked_keys)
            or (evidence.action is AcknowledgedAction.PASS and key in passed_keys)
            or (evidence.action is AcknowledgedAction.PASS and key in locked_keys)
            or (evidence.action is AcknowledgedAction.LOCK and key in passed_keys)
            or _conflicting_lock(evidence, locked_slots, locked_players)
        )
        if conflict:
            blocking.append(PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT)
            continue
        if evidence.action is AcknowledgedAction.LOCK:
            slot_index = evidence.slot_index
            slot_position = evidence.slot_position
            accepted_score = evidence.accepted_fantasy_score
            if slot_index is None or slot_position is None or accepted_score is None:
                blocking.append(PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT)
                continue
            locked_slots[slot_index] = evidence.player_id
            locked_players[evidence.player_id] = evidence.game_id
            locked_keys.add(key)
            fixed.append(
                FixedSlot(
                    slot_index=slot_index,
                    slot_position=slot_position,
                    player_id=evidence.player_id,
                    game_id=evidence.game_id,
                    accepted_fantasy_score=accepted_score,
                    decision_time=evidence.decided_at,
                    decision_id=evidence.decision_id,
                    provenance=evidence.provenance,
                )
            )
            continue
        passed_keys.add(key)
        passed.append(
            PassedOpportunity(
                player_id=evidence.player_id,
                game_id=evidence.game_id,
                decision_time=evidence.decided_at,
                decision_id=evidence.decision_id,
                provenance=evidence.provenance,
            )
        )
    return tuple(fixed), tuple(passed)


def _invalid_lock_slot(
    evidence: AcknowledgedDecisionEvidence,
    slot_positions: Mapping[int, str],
    positions_by_player: Mapping[str, tuple[str, ...]],
) -> bool:
    if evidence.action is not AcknowledgedAction.LOCK:
        return False
    if evidence.slot_index is None or evidence.slot_position is None:
        return True
    if slot_positions.get(evidence.slot_index) != evidence.slot_position:
        return True
    return not eligible_for_slot(
        positions_by_player.get(evidence.player_id, ()), evidence.slot_position
    )


def _conflicting_lock(
    evidence: AcknowledgedDecisionEvidence,
    locked_slots: Mapping[int, str],
    locked_players: Mapping[str, str],
) -> bool:
    if evidence.action is not AcknowledgedAction.LOCK:
        return False
    slot_index = evidence.slot_index
    if slot_index is None or evidence.accepted_fantasy_score is None:
        return True
    return locked_slots.get(slot_index) not in (None, evidence.player_id) or locked_players.get(
        evidence.player_id
    ) not in (None, evidence.game_id)


def _freshness_summary(inputs: LivePlanningInputs) -> FreshnessSummary:
    profile = inputs.league_profile
    sources = [
        SourceLineage(
            source="sleeper",
            version=profile.configuration_fingerprint,
            available_as_of=profile.retrieved_at,
            retrieved_at=profile.retrieved_at,
        )
    ]
    sources.extend(
        _resource_lineage(result.resource, result.quality) for result in inputs.schedule_results
    )
    sources.extend(
        _resource_lineage(result.resource, result.quality) for result in inputs.availability_results
    )
    sources.extend(
        SourceLineage(
            source=report.resource,
            version=report.state.value,
            available_as_of=report.retrieved_at,
            retrieved_at=report.retrieved_at,
        )
        for report in inputs.identity_quality_reports
    )
    return FreshnessSummary(tuple(sources))


def _resource_lineage(resource: str, quality: DataQualityReport) -> SourceLineage:
    return SourceLineage(
        source=resource,
        version=quality.state.value,
        available_as_of=quality.retrieved_at,
        retrieved_at=quality.retrieved_at,
    )


def _combined_version(versions: set[str]) -> str:
    if not versions:
        return "unknown"
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed:" + ",".join(sorted(versions))


def _missing_eligibility(
    roster_player_ids: tuple[str, ...],
    positions_by_player: dict[str, tuple[str, ...]],
) -> bool:
    return any(player_id not in positions_by_player for player_id in roster_player_ids)


def _unique_reasons(reasons: list[PlanningReasonCode]) -> tuple[PlanningReasonCode, ...]:
    return tuple(dict.fromkeys(reasons))


def _clean_optional_ids(values: tuple[str | None, ...]) -> tuple[str | None, ...]:
    cleaned: list[str | None] = []
    for value in values:
        if value is None:
            cleaned.append(None)
            continue
        normalized = value.strip()
        cleaned.append(normalized if normalized and normalized != "0" else None)
    return tuple(cleaned)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise PlanningInputsError(f"{label} must be timezone-aware")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise PlanningInputsError(f"{label} must be non-empty")


__all__ = (
    "AvailabilityResourceResult",
    "FantasyWeekWindow",
    "LivePlanningInputs",
    "LiveProjectionResult",
    "PlanningFreshnessPolicy",
    "PlanningInputsError",
    "PlayerEligibilityEvidence",
    "ResolvedPlayerIdentity",
    "ScheduleResourceResult",
    "build_live_team_week_state",
)
