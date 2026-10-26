"""Refresh recovery evidence from observed rosters instead of assuming proposed swaps occurred."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.league import RosterSlot
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
    PlanningReasonCode,
    PlanStatus,
)
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.base import DueWorkKind, RecommendationRecord
from sleeper_manager.workflows.daily_plan import run_daily_plan
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LiveProjectionResult
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import _repository
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _game,
    _inputs,
    _profile,
    _schedule_result,
)
from tests.sleeper_manager.workflows.postgame_lock_in_support import _expected_snapshot
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender

POLICY = WeeklyPlanPolicyConfig(scenario_count=16, postgame_scenario_count=8)


def _trial_inputs():
    profile = replace(
        _profile(starter_ids=("p1",)),
        roster_slots=(RosterSlot(0, "UTIL", True), RosterSlot(1, "BN", False)),
    )
    later = _game("later", start=NOW + timedelta(hours=3))
    early = replace(
        _game("early", start=NOW + timedelta(hours=1)), home_team_id="20", away_team_id="21"
    )
    inputs = _inputs(
        profile,
        schedule_results=(_schedule_result(later, early),),
        projections=(
            LiveProjectionResult("p1", "later", _expected_snapshot("p1", "later", 60), None),
            LiveProjectionResult("p2", "early", _expected_snapshot("p2", "early", 10), None),
        ),
    )
    return replace(
        inputs,
        identities=(inputs.identities[0], replace(inputs.identities[1], provider_team_id="20")),
    )


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_recovery_stays_pending_until_the_observed_lineup_changes(
    backend: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        inputs = _trial_inputs()

        async def run(evidence, at):
            notifications._clock = lambda: at
            return await run_daily_plan(
                evidence,
                decision_time=at,
                repository=repository,
                notifications=notifications,
                open_sleeper_url="https://example.test",
                policy=POLICY,
                clock=lambda: at,
            )

        first = await run(inputs, NOW)
        assert first.plan.recovery_obligations
        assert first.plan.desired_assignments[0].player_id == "p2"
        assert (await run(inputs, NOW)).outcome == "duplicate"
        checks = await repository.list_scheduled_work(kind=DueWorkKind.DAILY)
        assert any(row.dedupe_key.startswith("recovery:") for row in checks)
        at = NOW + timedelta(hours=2, minutes=40)
        active = replace(
            inputs,
            league_profile=replace(
                inputs.league_profile,
                retrieved_at=at,
                rosters=(replace(inputs.league_profile.rosters[0], starter_ids=("p2",)),),
            ),
            schedule_results=tuple(
                replace(
                    result,
                    games=tuple(
                        replace(game, status=GameStatus.IN_PROGRESS)
                        if game.provider_id == "early"
                        else game
                        for game in result.games
                    ),
                )
                for result in inputs.schedule_results
            ),
        )
        restore = await run(active, at)
        assert restore.plan.desired_assignments[0].player_id == "p1"
        assert restore.plan.forfeited_opportunities == (("p2", "early"),)
        assert restore.plan.recovery_obligations
        assert restore.plan.fixed_slots == restore.plan.passed_opportunities == ()
        assert (await run(active, at)).outcome == "duplicate"
        assert len(sender.messages) == 2
        restored = replace(
            active,
            league_profile=replace(
                active.league_profile,
                rosters=(replace(active.league_profile.rosters[0], starter_ids=("p1",)),),
            ),
        )
        observed = await run(restored, at)
        assert observed.outcome == "no_action"
        assert observed.plan.recovery_obligations == observed.plan.forfeited_opportunities == ()
        assert len(sender.messages) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_conflicting_confirmed_position_returns_observed_lineup_without_moves(
    backend: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        evidence = AcknowledgedDecisionEvidence(
            "confirmed", "p1", "done", AcknowledgedAction.LOCK, NOW, "manager", 0, "PG", 30
        )
        inputs = _inputs(
            _profile(starter_ids=(None, "p1")),
            schedule_results=(
                _schedule_result(
                    _game("done", start=NOW - timedelta(hours=3), status=GameStatus.FINAL)
                ),
            ),
            acknowledgements=(evidence,),
        )
        result = await run_daily_plan(
            inputs,
            decision_time=NOW,
            repository=repository,
            notifications=notifications,
            open_sleeper_url="https://example.test",
            policy=POLICY,
            clock=lambda: NOW,
        )
        assert result.plan.status is PlanStatus.BLOCKED
        assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in result.plan.blocking_reasons
        assert result.plan.desired_assignments == result.plan.observed_assignments
        assert result.plan.moves == ()
        assert result.plan.fixed_slots[0].slot_index == 0
        assert "Advice is blocked" in result.recommendation.message
        trace = json.loads(result.recommendation.trace_json)
        assert trace["observed_assignments"]

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_legacy_trace_does_not_invent_recovery_provenance(backend: str, tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        await repository.create_recommendation(
            RecommendationRecord(
                "legacy",
                "legacy",
                "league-1",
                1,
                "weekly-lineup",
                None,
                "weekly_lineup",
                "Old plan",
                "Old advice",
                NOW + timedelta(days=1),
                "old",
                NOW,
                trace_json='{"schema_version":2}',
            )
        )
        inputs = _trial_inputs()
        at = NOW + timedelta(hours=2, minutes=40)
        active = replace(
            inputs,
            league_profile=replace(
                inputs.league_profile,
                retrieved_at=at,
                rosters=(replace(inputs.league_profile.rosters[0], starter_ids=("p2",)),),
            ),
            schedule_results=tuple(
                replace(
                    result,
                    games=tuple(
                        replace(game, status=GameStatus.IN_PROGRESS)
                        if game.provider_id == "early"
                        else game
                        for game in result.games
                    ),
                )
                for result in inputs.schedule_results
            ),
        )
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(RecordingSender()),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: at,
        )
        result = await run_daily_plan(
            active,
            decision_time=at,
            repository=repository,
            notifications=notifications,
            open_sleeper_url="https://example.test",
            policy=POLICY,
            clock=lambda: at,
        )
        assert result.plan.recovery_obligations == result.plan.forfeited_opportunities == ()
        assert result.plan.desired_assignments[0].player_id == "p2"
        assert result.plan.expected_terminal_score is None

    asyncio.run(exercise())
