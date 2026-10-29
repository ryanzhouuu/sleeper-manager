"""Saved decisions survive configuration refreshes and elapsed acceptance windows."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.workflows.lock_in_evidence import evidence_from_lock_in_opportunity
from sleeper_manager.workflows.lock_in_planning import (
    _merge_planning_evidence,
    sync_live_lock_in_opportunities,
)
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision
from sleeper_manager.workflows.recommendation_state import refreshed_team_state
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import (
    _opportunity,
    _repository,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _game,
    _inputs,
    _profile,
    _schedule_result,
)


@pytest.mark.parametrize(
    "status", (Status.ACKNOWLEDGED_LOCKED, Status.ACKNOWLEDGED_PASSED, Status.AUTOMATIC_FINAL)
)
def test_configuration_refresh_preserves_settled_decisions(status: Status) -> None:
    current = replace(_opportunity(), status=status, stable_score=54.9)
    candidate = replace(current, league_configuration_fingerprint="changed-config")

    refreshed = _merge_planning_evidence(current, candidate)

    assert refreshed.status is status
    assert refreshed.league_configuration_fingerprint == "changed-config"
    assert refreshed.stable_score == 54.9


def test_configuration_refresh_still_requires_reconciliation_for_open_games() -> None:
    current = _opportunity()
    candidate = replace(current, league_configuration_fingerprint="changed-config")

    assert _merge_planning_evidence(current, candidate).status is Status.RECONCILIATION_REQUIRED


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
@pytest.mark.parametrize(
    "stored_status", (None, Status.EXPIRED, Status.INELIGIBLE, Status.RECONCILIATION_REQUIRED)
)
@pytest.mark.parametrize(
    ("status", "action"),
    ((Status.ACKNOWLEDGED_LOCKED, "locked"), (Status.ACKNOWLEDGED_PASSED, "passed")),
)
def test_saved_decision_remains_in_planning_after_refresh_and_expiry(
    backend: str, stored_status: Status | None, status: Status, action: str, tmp_path: Path
) -> None:
    """A later game and configuration change cannot reopen an accepted decision."""

    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        game = _game("g1", start=NOW - timedelta(hours=3), status=GameStatus.FINAL)
        later = _game("g2", start=NOW - timedelta(minutes=30), status=GameStatus.IN_PROGRESS)
        inputs = _inputs(
            _profile(retrieved_at=NOW),
            schedule_results=(_schedule_result(game, later),),
        )
        saved = replace(
            _opportunity(),
            key=replace(
                _opportunity().key, league_id="league-1", roster_id=1, player_id="p1", game_id="g1"
            ),
            status=stored_status or status,
            scheduled_start=game.start_time,
            action_deadline=later.start_time - inputs.move_lead_time,
            fantasy_week_end=inputs.week_window.ends_at,
            slot_index=0,
            slot_position="PG",
            league_configuration_fingerprint="old-config",
            roster_evidence_at=game.start_time - timedelta(minutes=5),
            stable_score=54.9,
            stable_fingerprint="stable",
            current_observation_fingerprint="stable",
            stabilized_at=NOW - timedelta(hours=2),
            acknowledged_action=action,
            acknowledged_at=NOW - timedelta(hours=2),
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(hours=2),
        )
        await repository.upsert_lock_in_opportunity(saved)

        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
        await repository.expire_lock_in_opportunities(NOW)
        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)

        stored = await repository.get_lock_in_opportunity(saved.key)
        assert stored is not None
        assert stored.status is status
        assert stored.acknowledged_action == action
        assert stored.acknowledged_at == saved.acknowledged_at
        assert (stored.slot_index, stored.slot_position) == (0, "PG")
        assert evidence_from_lock_in_opportunity(stored) is not None
        assert stored in await repository.load_acknowledged_lock_in_opportunities("league-1", 1)

        state = await refreshed_team_state(inputs, repository, NOW)
        assert not state.is_blocked
        if action == "locked":
            assert len(state.fixed_slots) == 1
            assert state.fixed_slots[0].player_id == "p1"
            assert state.fixed_slots[0].accepted_fantasy_score == 54.9
            plan = build_team_recommendation(state)
            assert plan.fixed_slots == state.fixed_slots
            assert all(row.player_id != "p1" for row in plan.score_actions)
            changed_slots = replace(
                inputs.league_profile,
                roster_slots=tuple(
                    replace(slot, position="SG") if slot.index == 0 else slot
                    for slot in inputs.league_profile.roster_slots
                ),
            )
            incompatible = await refreshed_team_state(
                replace(inputs, league_profile=changed_slots), repository, NOW
            )
            assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in incompatible.blocking_reasons
        else:
            assert [(row.player_id, row.game_id) for row in state.passed_opportunities] == [
                ("p1", "g1")
            ]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("status", "action"),
    ((Status.ACKNOWLEDGED_LOCKED, "locked"), (Status.ACKNOWLEDGED_PASSED, "passed")),
)
def test_wait_correction_is_not_restored_by_refresh(status: Status, action: str) -> None:
    current = replace(_opportunity(), status=status, acknowledged_action=action)
    corrected = reported_decision(
        current, ManagerAction.NOT_LOCKED, current.updated_at, actual_slot=None
    )
    candidate = replace(corrected, league_configuration_fingerprint="changed-config")

    refreshed = _merge_planning_evidence(corrected, candidate)

    assert refreshed.status is Status.RECONCILIATION_REQUIRED
    assert refreshed.acknowledged_action is None
    assert evidence_from_lock_in_opportunity(refreshed) is None
