"""Postgame score collection must finish before the team optimizer starts."""

import asyncio
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

from sleeper_manager.cloudflare import postgame_dispatch
from sleeper_manager.cloudflare.dispatcher import dispatch_due_work
from sleeper_manager.cloudflare.scheduler_types import ScheduledRunStatus
from sleeper_manager.persistence.base import DueWorkKind, RuntimePolicyRecord
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import _assembly, _collector
from tests.sleeper_manager.workflows.postgame_lock_in_support import (
    NOW,
    POLICY,
    POSTGAME,
    _box,
    _prepare,
    _summary_result,
)


def test_all_due_finals_are_saved_before_one_team_plan(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A failed optimizer cannot prevent later games from accepting their first final."""

    async def exercise() -> None:
        repository, inputs, notifications, _ = await _prepare(tmp_path)
        first, second = inputs.schedule_results[0].games
        inputs = replace(
            inputs,
            identities=(inputs.identities[0], replace(inputs.identities[1], provider_team_id="30")),
            schedule_results=(
                replace(
                    inputs.schedule_results[0],
                    games=(
                        first,
                        replace(
                            second,
                            start_time=first.start_time,
                            home_team_id="30",
                            away_team_id="31",
                        ),
                    ),
                ),
            ),
        )
        await repository.save_runtime_policy(
            RuntimePolicyRecord(POLICY.version, POLICY.to_json(), NOW)
        )
        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
        assembly = replace(_assembly(), evidence=CollectedLiveEvidence(inputs, POSTGAME, ()))
        collect, _ = _collector(assembly)
        fetched: list[str] = []
        planner_calls: list[set[str]] = []

        async def fetch(game_id: str):
            """Return a complete final with both fixture players present."""
            fetched.append(game_id)
            result = _summary_result(_box("401", points=20), _box("402", points=8))
            return replace(
                result,
                records=replace(
                    result.records, game=replace(result.records.game, provider_id=game_id)
                ),
            )

        async def fail_after_observations(inputs, stable, **kwargs):
            """Check persisted finals at the point where planning can fail."""
            del inputs, kwargs
            planner_calls.append({row.key.game_id for row in stable})
            for row in stable:
                saved = await repository.get_lock_in_opportunity(row.key)
                assert saved is not None
                assert saved.stable_score is not None
                assert saved.consecutive_direct_poll_count == 1
            raise RuntimeError("optimizer unavailable")

        monkeypatch.setattr(postgame_dispatch, "recommend_postgame_batch", fail_after_observations)
        result = await dispatch_due_work(
            repository,
            notifications=notifications,
            collect=collect,
            scheduled_at=POSTGAME,
            correlation_id="postgame-batch",
            open_sleeper_url="https://example.test",
            fetch_game_summary=fetch,
        )
        assert sorted(fetched) == ["g1", "g2"]
        assert planner_calls == [{"g1", "g2"}]
        postgame_attempts = [
            attempt for attempt in result.attempts if attempt.kind is DueWorkKind.POSTGAME
        ]
        assert [attempt.outcome for attempt in postgame_attempts] == [ScheduledRunStatus.FAILED] * 2

    asyncio.run(exercise())
