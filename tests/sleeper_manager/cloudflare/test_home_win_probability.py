import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sleeper_manager.cloudflare.home_win_probability import matchup_win_probability
from sleeper_manager.cloudflare.live_score import LockedGame, SideScore
from sleeper_manager.cloudflare.opponent_outlook import OpponentGame, OpponentOutlook
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.domain.scoring import ScoringPolicy

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
SCORING = ScoringPolicy.from_sleeper({"pts": 1})
TRACE = {"terminal_score_samples": [100.0, 110.0, 120.0, 130.0]}
LAST_NIGHT = datetime(2026, 1, 7, 1, tzinfo=UTC)
TONIGHT = datetime(2026, 1, 8, 1, tzinfo=UTC)


class MemoryCache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], LockedGame] = {}

    async def get(self, player_id: str, game_id: str) -> LockedGame | None:
        return self.rows.get((player_id, game_id))

    async def put(self, player_id: str, game_id: str, game: LockedGame) -> None:
        self.rows[(player_id, game_id)] = game


class Stats:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.reads: list[str] = []

    async def __call__(self, player_id: str, season: int) -> dict[str, Any]:
        self.reads.append(player_id)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _stats(points: float | None) -> dict[str, Any]:
    stats = {} if points is None else {"pts": points}
    return {"2026": [{"date": "2026-01-06", "game_id": "last-night", "stats": stats}]}


def _opponent(points: float = 90.0, *, locked: frozenset[str] = frozenset()) -> SideScore:
    return SideScore(
        points, False, len(locked), 3, False, starter_ids=("a", "b", "0"), locked_ids=locked
    )


def _outlook(**players: tuple[OpponentGame, ...] | None) -> OpponentOutlook:
    return OpponentOutlook(2, NOW - timedelta(hours=8), players)


def _run(
    outlook: OpponentOutlook | None,
    opponent: SideScore | None = None,
    *,
    trace: dict[str, Any] | None = TRACE,
    stats: Stats | None = None,
    cache: MemoryCache | None = None,
    roster_id: int = 2,
) -> float | None:
    return asyncio.run(
        matchup_win_probability(
            trace,
            outlook,
            roster_id,
            opponent or _opponent(),
            read_stats=stats or Stats(_stats(None)),
            season=2025,
            scoring=SCORING,
            cache=cache,
            now=NOW,
        )
    )


def test_locked_opponent_is_compared_with_every_simulated_finish() -> None:
    probability = _run(_outlook(), _opponent(110.0, locked=frozenset({"a", "b"})))

    assert probability == 0.625


def test_upcoming_games_add_the_opponent_starters_spread() -> None:
    upcoming = (OpponentGame(TONIGHT, (0.0, 20.0)),)

    probability = _run(_outlook(a=upcoming, b=upcoming))

    assert probability == pytest.approx(0.6053, abs=1e-4)


def test_a_finished_unlocked_game_is_read_once_and_cached() -> None:
    games = (OpponentGame(LAST_NIGHT, None), OpponentGame(TONIGHT, (0.0, 20.0)))
    stats, cache = Stats(_stats(30.0)), MemoryCache()

    first = _run(_outlook(a=games, b=()), stats=stats, cache=cache)
    second = _run(_outlook(a=games, b=()), stats=stats, cache=cache)

    assert first == second == 0.375
    assert stats.reads == ["a"]


def test_a_game_still_in_progress_uses_its_projection() -> None:
    started = NOW - timedelta(hours=1)
    stats = Stats(_stats(30.0))

    probability = _run(_outlook(a=(OpponentGame(started, (20.0,)),), b=()), stats=stats)

    assert probability == 0.625
    assert stats.reads == []


def test_a_finished_game_without_stats_scores_zero() -> None:
    probability = _run(
        _outlook(a=(OpponentGame(LAST_NIGHT, None),), b=()), stats=Stats(_stats(None))
    )

    assert probability == 1.0


@pytest.mark.parametrize(
    ("outlook", "stats"),
    [
        (None, None),
        (_outlook(a=None, b=()), None),
        (_outlook(b=()), None),
        (_outlook(a=(OpponentGame(LAST_NIGHT, None),), b=()), Stats({"2026": []})),
        (
            _outlook(a=(OpponentGame(LAST_NIGHT, None),), b=()),
            Stats(CloudflareProviderError("down")),
        ),
    ],
)
def test_unknown_opponent_scores_hide_the_probability(
    outlook: OpponentOutlook | None, stats: Stats | None
) -> None:
    assert _run(outlook, stats=stats) is None


def test_missing_plan_samples_or_another_opponent_hide_the_probability() -> None:
    locked = _opponent(locked=frozenset({"a", "b"}))

    assert _run(_outlook(), locked, trace=None) is None
    assert _run(_outlook(), locked, trace={"terminal_score_samples": ["x"]}) is None
    assert _run(_outlook(), locked, roster_id=3) is None
    assert _run(_outlook(), SideScore(90.0, True, 2, 3, False)) is None
