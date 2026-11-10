import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from sleeper_manager.cloudflare.decision_objective import planning_win_objective
from sleeper_manager.cloudflare.home_cache import write_home_cache
from sleeper_manager.cloudflare.opponent_outlook import outlook_cache_key
from sleeper_manager.decisions.win_probability import Finish, WinObjective
from sleeper_manager.domain.runtime_policy import RuntimePolicy, default_runtime_policy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from tests.sleeper_manager.workflows.test_daily_plan import NOW, _profile

POINTS = default_runtime_policy(history_version="history-v1")
WIN_CHANCE = replace(POINTS, manager_intent=replace(POINTS.manager_intent, objective="win_chance"))
TONIGHT = NOW + timedelta(hours=6)


class Reader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.leg_reads = 0

    async def matchup_legs(self, league_id: str, week: int) -> object:
        self.leg_reads += 1
        if self.fail:
            raise RuntimeError("graphql down")
        return [
            {"roster_id": 1, "matchup_id": 4, "points": None, "starters": ["p1"]},
            {
                "roster_id": 2,
                "matchup_id": 4,
                "points": None,
                "starters": ["o1", "o2"],
                "starters_games": {"o1": "g-locked"},
            },
        ]

    async def player_game_stats(self, player_id: str, season: int) -> dict[str, Any]:
        return {"2025": [{"game_id": "g-locked", "date": "2026-01-06", "stats": {"pts": 40}}]}


def _objective(tmp_path, policy: RuntimePolicy, reader: Reader | None, *, outlook: bool = True):  # type: ignore[no-untyped-def]
    async def exercise() -> WinObjective | None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        profile = _profile()
        if outlook:
            await write_home_cache(
                repository,
                outlook_cache_key(profile.league_id, profile.fantasy_week.week),
                {
                    "roster_id": 2,
                    "projected_at": NOW.isoformat(),
                    "players": {"o2": [{"start": TONIGHT.isoformat(), "outcomes": [10, 30]}]},
                },
                resource="opponent-outlook",
                timezone_name=policy.manager_timezone,
                now=NOW,
            )
        return await planning_win_objective(
            policy, repository=repository, reader=reader, profile=profile, now=NOW
        )

    return asyncio.run(exercise())


def test_win_chance_policy_scores_the_opponents_locks_and_open_starters(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert _objective(tmp_path, WIN_CHANCE, Reader()) == WinObjective(Finish(60.0, 100.0))


def test_points_policy_reads_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reader = Reader()

    assert _objective(tmp_path, POINTS, reader) is None
    assert reader.leg_reads == 0


def test_missing_evidence_falls_back_to_points(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert _objective(tmp_path / "a", WIN_CHANCE, Reader(), outlook=False) is None
    assert _objective(tmp_path / "b", WIN_CHANCE, None) is None
    assert _objective(tmp_path / "c", WIN_CHANCE, Reader(fail=True)) is None
