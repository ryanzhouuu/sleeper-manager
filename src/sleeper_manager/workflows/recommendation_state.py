"""Refresh confirmations, retained scores, and proposed recovery evidence for every trigger."""

import json
from dataclasses import replace
from datetime import datetime

from pydantic import TypeAdapter

from sleeper_manager.domain.planning import PlanningReasonCode, TeamWeekState
from sleeper_manager.domain.planning_actions import RecoveryObligation
from sleeper_manager.persistence.base import AsyncStateRepository
from sleeper_manager.workflows.lock_in_evidence import merge_lock_in_opportunity_evidence
from sleeper_manager.workflows.planning_history import player_game_evidence
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs, build_live_team_week_state

_RECOVERIES = TypeAdapter(tuple[RecoveryObligation, ...])


async def refreshed_team_state(
    inputs: LivePlanningInputs,
    repository: AsyncStateRepository,
    decision_time: datetime,
) -> TeamWeekState:
    """Read manager decisions again; loading a recommendation never asserts its moves happened."""
    profile = inputs.league_profile
    acknowledgements = await repository.load_acknowledged_decisions(
        profile.league_id, inputs.week_window.week, as_of=decision_time
    )
    load = getattr(repository, "list_week_lock_in_opportunities", None)
    history = await load(profile.league_id, inputs.week_window.week) if load else ()
    by_key = {(row.player_id, row.game_id): row for row in inputs.acknowledgements}
    by_key.update({(row.player_id, row.game_id): row for row in acknowledgements})
    refreshed = replace(
        inputs,
        acknowledgements=merge_lock_in_opportunity_evidence(tuple(by_key.values()), history),
        player_game_evidence=tuple(player_game_evidence(row) for row in history)
        or inputs.player_game_evidence,
    )
    state = build_live_team_week_state(refreshed, decision_time=decision_time)
    observed = {row.slot_index: row.player_id for row in state.observed_starters}
    if any(observed.get(row.slot_index) != row.player_id for row in state.fixed_slots):
        state = replace(
            state,
            blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT),
        )
    latest = await repository.latest_current_recommendation(
        profile.league_id, inputs.week_window.week, decision_type="weekly_lineup"
    )
    if latest is None:
        return state
    try:
        trace = json.loads(latest.trace_json)
        if not isinstance(trace, dict):
            raise ValueError("Stored team-plan evidence must be an object")
        if (
            trace.get("schema_version") != 3
            or trace.get("roster_id") != profile.manager_roster_id
            or trace.get("season") != profile.season
            or trace.get("league_configuration_version") != profile.configuration_fingerprint
        ):
            return state
        recoveries = _RECOVERIES.validate_python(trace.get("recovery_obligations", []))
        recoveries = tuple(row for row in recoveries if row.created_at <= decision_time)
        failures = TypeAdapter(tuple[str, ...]).validate_python(trace.get("recovery_failures", []))
        return replace(state, recovery_obligations=recoveries, recovery_failures=failures)
    except (ValueError, TypeError):
        return replace(
            state,
            blocking_reasons=(*state.blocking_reasons, PlanningReasonCode.AMBIGUOUS_ELIGIBILITY),
        )
