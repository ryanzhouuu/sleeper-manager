"""Remaining-game absences change stable postgame continuation advice and its explanation."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_availability import constrain_live_availability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.postgame_lock_in import run_postgame_lock_in
from tests.sleeper_manager.workflows import postgame_lock_in_support as fixture


def test_future_confirmed_absence_changes_pass_to_lock_and_is_visible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, inputs, notifications, sender = await fixture._prepare(tmp_path)
        inputs = replace(
            inputs,
            projections=tuple(
                replace(row, snapshot=fixture._expected_snapshot("p1", "g2", 30))
                if (row.sleeper_player_id, row.game_id) == ("p1", "g2")
                else row
                for row in inputs.projections
            ),
        )
        fetch = fixture._fetcher(
            fixture._summary_result(
                fixture._box("401", points=5), fixture._box("402", points=8, did_play=False)
            )
        )

        async def poll(at, identity, evidence):  # type: ignore[no-untyped-def]
            notifications._clock = lambda: at
            return await run_postgame_lock_in(
                "g1",
                evidence,
                decision_time=at,
                repository=repository,
                notifications=notifications,
                fetch_summary=fetch,
                runtime_policy=fixture.POLICY,
                open_sleeper_url="https://sleeper.com",
                poll_id=identity,
                player_names={"p1": "Ann", "p2": "Ben"},
            )

        await poll(fixture.POSTGAME, "first", inputs)
        await poll(fixture.SECOND_POLL, "second", inputs)
        key = LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        before = await repository.get_lock_in_opportunity(key)
        assert json.loads(before.trace_json)["kind"] == "pass"
        at = fixture.SECOND_POLL + timedelta(minutes=5)
        report = GameAvailability(
            "g2",
            at,
            AvailabilityStatus.OUT,
            AvailabilityObservation.REPORTED,
            "official:private",
            True,
        )
        revised = replace(
            inputs,
            projections=tuple(
                replace(
                    row,
                    snapshot=constrain_live_availability(
                        row.snapshot,
                        LiveProjectionTarget("p1", "g2", fixture.LATER_START),
                        report,
                        cutoff=at,
                        scoring_policy=inputs.league_profile.scoring,
                        max_age=timedelta(minutes=15),
                    ),
                )
                if (row.sleeper_player_id, row.game_id) == ("p1", "g2")
                else row
                for row in inputs.projections
            ),
        )
        await poll(at, "status-change", revised)
        after = await repository.get_lock_in_opportunity(key)
        trace = json.loads(after.trace_json)
        assert trace["kind"] == "lock"
        assert trace["availability_notes"]
        assert any("• Out: Ann" in message.message for message in sender.messages)
        assert "Out for this game" in trace["availability_notes"][0]
        assert all("private" not in message.message for message in sender.messages)

    asyncio.run(exercise())
