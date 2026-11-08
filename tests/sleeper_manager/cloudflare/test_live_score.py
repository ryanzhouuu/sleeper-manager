import asyncio
from collections.abc import Mapping
from datetime import date
from typing import Any

from sleeper_manager.cloudflare.live_score import SideScore, side_score
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.sleeper.matchup_legs import MatchupLeg

POLICY = ScoringPolicy.from_sleeper(
    {
        "ast": 1.5,
        "blk": 2,
        "bonus_ast_15p": 2,
        "bonus_pt_40p": 2,
        "bonus_pt_50p": 2,
        "bonus_reb_20p": 2,
        "dd": 1,
        "ff": -2,
        "pts": 1,
        "reb": 1.2,
        "stl": 2,
        "td": 2,
        "tf": -2,
        "to": -1,
        "tpm": 0.5,
    }
)
STARTERS = tuple(f"20{index:02d}" for index in range(90, 99))
BIG_GAME = {"pts": 40.0, "reb": 5.0, "ast": 5.0, "stl": 2.0, "to": 4.0, "tpm": 5.0}


def leg(
    *, points: float | None = None, starters_games: Mapping[str, str] | None = None
) -> MatchupLeg:
    return MatchupLeg(
        roster_id=6,
        matchup_id=1,
        points=points,
        starters=STARTERS,
        starters_games=starters_games or {},
    )


def stats(rows: Mapping[str, list[dict[str, Any]]]) -> Any:
    calls: list[str] = []

    async def read(player_id: str, season: int) -> dict[str, Any]:
        assert season == 2026
        calls.append(player_id)
        return {"3": rows.get(player_id, [])}

    read.calls = calls  # type: ignore[attr-defined]
    return read


async def no_reads(player_id: str, season: int) -> dict[str, Any]:
    raise AssertionError("Final points need no stat reads")


def score(value: MatchupLeg, read_stats: Any) -> SideScore:
    return asyncio.run(side_score(value, read_stats=read_stats, season=2026, scoring=POLICY))


def test_side_score_uses_final_points_when_sleeper_scored_the_week() -> None:
    assert score(leg(points=383.0), no_reads) == SideScore(383.0, True, 0, 9, False)


def test_side_score_sums_only_locked_games() -> None:
    reader = stats(
        {
            "2090": [
                {"game_id": "g-early", "stats": {"pts": 10.0}},
                {"game_id": "g-nop", "stats": BIG_GAME},
            ],
            "2091": [{"game_id": "g-unlocked", "stats": {"pts": 30.0}}],
        }
    )

    assert score(leg(starters_games={"2090": "g-nop"}), reader) == SideScore(
        58.0, False, 1, 9, False
    )
    assert reader.calls == ["2090"]


def test_side_score_with_no_locks_is_zero() -> None:
    assert score(leg(), no_reads) == SideScore(0.0, False, 0, 9, False)


def test_side_score_is_pending_when_a_locked_game_has_no_stats_row() -> None:
    result = score(leg(starters_games={"2090": "g-missing"}), stats({}))

    assert result == SideScore(None, False, 1, 9, True)


def test_side_score_is_pending_when_a_locked_game_has_empty_stats() -> None:
    reader = stats({"2090": [{"game_id": "g-nop", "stats": {}}]})

    assert score(leg(starters_games={"2090": "g-nop"}), reader).pending is True


def test_side_score_reports_each_locked_games_eastern_date() -> None:
    reader = stats({"2090": [{"game_id": "g-nop", "date": "2026-11-05", "stats": BIG_GAME}]})

    result = score(leg(starters_games={"2090": "g-nop"}), reader)

    assert dict(result.lock_dates) == {"2090": date(2026, 11, 5)}
