"""Availability changes reach visible lineup advice and durable recommendation revisions."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.league import RosterSlot
from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_availability import constrain_live_availability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.daily_plan import run_daily_plan
from tests.sleeper_manager.workflows.test_daily_plan import (
    NOW,
    _inputs,
    _profile,
    _projections,
    _workflow,
)


def reported(inputs, player: str, status: AvailabilityStatus, *, at=NOW, source="official:private"):  # type: ignore[no-untyped-def]
    starts = {
        game.provider_id: game.start_time
        for result in inputs.schedule_results
        for game in result.games
    }
    report = GameAvailability(
        "g1",
        at,
        status,
        AvailabilityObservation.REPORTED,
        source,
        confirmed_absence=status is AvailabilityStatus.OUT,
    )
    projections = tuple(
        replace(
            row,
            snapshot=constrain_live_availability(
                row.snapshot,
                LiveProjectionTarget(player, row.game_id, starts[row.game_id]),
                report,
                cutoff=NOW,
                scoring_policy=inputs.league_profile.scoring,
                max_age=timedelta(minutes=15),
            ),
        )
        if row.sleeper_player_id == player
        else row
        for row in inputs.projections
    )
    return replace(inputs, projections=projections)


async def run(inputs, repository, notifications):  # type: ignore[no-untyped-def]
    return await run_daily_plan(
        inputs,
        decision_time=NOW,
        repository=repository,
        notifications=notifications,
        open_sleeper_url="https://sleeper.com",
        player_names={"p1": "Ann", "p2": "Ben"},
        policy=WeeklyPlanPolicyConfig(scenario_count=8),
        clock=lambda: NOW,
    )


def test_changed_questionable_warning_revises_advice_and_identical_warning_deduplicates(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        inputs = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10, p2_expected=30),
        )
        first = await run(inputs, repository, notifications)
        uncertain = await run(
            reported(inputs, "p2", AvailabilityStatus.QUESTIONABLE), repository, notifications
        )
        repeated = await run(
            reported(inputs, "p2", AvailabilityStatus.QUESTIONABLE, source="official:second"),
            repository,
            notifications,
        )
        assert first.outcome == uncertain.outcome == "notified"
        assert repeated.outcome == "duplicate"
        assert len(sender.messages) == 2
        assert "Questionable" in uncertain.notification.message
        assert "private" not in uncertain.notification.message
        assert first.plan.material_hash == uncertain.plan.material_hash
        assert first.recommendation.idempotency_key != uncertain.recommendation.idempotency_key
        trace = json.loads(uncertain.recommendation.trace_json)
        assert trace["availability_notes"]
        assert trace["availability_material_hash"] != trace["material_hash"]

    asyncio.run(exercise())


def test_confirmed_absence_changes_lineup_and_late_clearance_withdraws_old_advice(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        profile = replace(
            _profile(starter_ids=("p1",)),
            roster_slots=(RosterSlot(0, "UTIL", True), RosterSlot(1, "BN", False)),
        )
        inputs = _inputs(profile, projections=_projections(p1_expected=40, p2_expected=10))
        first = await run(inputs, repository, notifications)
        out = await run(reported(inputs, "p1", AvailabilityStatus.OUT), repository, notifications)
        assert first.outcome == "no_action"
        assert out.outcome == "notified"
        assert any(
            move.player_id == "p2" and move.target_slot_index == 0 for move in out.plan.moves
        )
        assert "Out for this game" in out.notification.message
        cleared = await run(
            reported(inputs, "p1", AvailabilityStatus.AVAILABLE), repository, notifications
        )
        assert cleared.outcome == "no_action"
        stored = await repository.get_recommendation(out.recommendation.recommendation_id)
        assert stored.status is RecommendationStatus.SUPERSEDED
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_stale_absence_cannot_produce_an_actionable_move(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        profile = replace(
            _profile(starter_ids=("p1",)),
            roster_slots=(RosterSlot(0, "UTIL", True), RosterSlot(1, "BN", False)),
        )
        inputs = _inputs(profile, projections=_projections(p1_expected=40, p2_expected=10))
        result = await run(
            reported(inputs, "p1", AvailabilityStatus.OUT, at=NOW - timedelta(minutes=16)),
            repository,
            notifications,
        )
        assert result.outcome == "no_action"
        assert sender.messages == []

    asyncio.run(exercise())


def test_missing_official_source_is_one_human_readable_warning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, _, notifications = _workflow(tmp_path)
        await repository.initialize()
        inputs = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10, p2_expected=30),
        )
        starts = {
            game.provider_id: game.start_time
            for result in inputs.schedule_results
            for game in result.games
        }
        inputs = replace(
            inputs,
            projections=tuple(
                replace(
                    row,
                    snapshot=constrain_live_availability(
                        row.snapshot,
                        LiveProjectionTarget(
                            row.sleeper_player_id, row.game_id, starts[row.game_id]
                        ),
                        None,
                        cutoff=NOW,
                        scoring_policy=inputs.league_profile.scoring,
                        max_age=timedelta(minutes=15),
                        availability_gap="official_availability_failed:RuntimeError",
                    ),
                )
                for row in inputs.projections
            ),
        )
        result = await run(inputs, repository, notifications)
        assert result.notification.message.count("Game availability could not be verified") == 1
        assert "RuntimeError" not in result.notification.message

    asyncio.run(exercise())
