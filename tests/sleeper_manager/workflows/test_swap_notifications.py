"""Daily and pre-tipoff recommendations persist and deliver atomic substitutions."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.league import RosterSlot
from sleeper_manager.workflows.daily_plan import run_daily_plan
from sleeper_manager.workflows.pre_tipoff_check import run_pre_tipoff_check
from tests.sleeper_manager.workflows.test_daily_plan import (
    NOW,
    _inputs,
    _profile,
    _projections,
    _workflow,
)


@pytest.mark.parametrize("trigger", ("daily", "pre_tipoff"))
def test_full_bench_substitution_delivers_one_swap(tmp_path: Path, trigger: str) -> None:
    async def exercise() -> None:
        profile = replace(
            _profile(starter_ids=("p1",)),
            roster_slots=(RosterSlot(0, "PG", True), RosterSlot(1, "BN", False)),
        )
        inputs = _inputs(profile, projections=_projections(p1_expected=10, p2_expected=40))
        inputs = replace(
            inputs,
            player_eligibility=(
                inputs.player_eligibility[0],
                replace(inputs.player_eligibility[1], eligible_positions=("PG",)),
            ),
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        workflow = run_daily_plan if trigger == "daily" else run_pre_tipoff_check

        result = await workflow(
            inputs,
            decision_time=NOW,
            repository=repository,
            notifications=notifications,
            open_sleeper_url="https://example.test/league",
            player_names={"p1": "Outgoing", "p2": "Incoming"},
            policy=WeeklyPlanPolicyConfig(scenario_count=8, postgame_scenario_count=8),
            clock=lambda: NOW,
        )

        assert len(sender.messages) == 1
        assert result.recommendation is not None
        assert (
            result.recommendation.message.split("\n\n")[0]
            == "Swap Incoming (bench) and Outgoing (PG)."
        )
        trace = json.loads(result.recommendation.trace_json)
        assert trace["schema_version"] == 3
        assert trace["trigger"] == trigger
        assert len(trace["moves"]) == 1
        assert trace["moves"][0]["kind"] == "swap"
        assert trace["moves"][0]["swap_player_id"] == "p1"

    asyncio.run(exercise())
