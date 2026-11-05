import asyncio
from datetime import UTC, datetime
from typing import Any

from sleeper_manager.cloudflare.providers import ProviderHTTPError
from sleeper_manager.cloudflare.sleeper_scoring import SleeperScoredSummarySource
from sleeper_manager.domain.nba import (
    DataQualityState,
    GameStatus,
    GameSummary,
    PlayerBoxScore,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
    quality_for_records,
)
from sleeper_manager.domain.scoring import BoxScoreLine

NOW = datetime(2026, 11, 5, 4, tzinfo=UTC)
# 11:30 PM Eastern on Nov 4 is already Nov 5 in UTC; Sleeper files the game under Nov 4.
LATE_START = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
ESPN_SOURCE = SourceMetadata("espn", "espn", NOW)
PLAYERS = {"espn-embiid": "1511"}
ROW = {"date": "2026-11-04", "stats": {"pts": 26.0, "to": 4.0, "tf": 1.0}}


def _box(player_id: str, *, did_play: bool = True) -> PlayerBoxScore:
    return PlayerBoxScore(
        game_id="g1",
        player_id=player_id,
        team_id="PHI",
        played_at=LATE_START,
        started=True,
        did_play=did_play,
        minutes=30.0 if did_play else None,
        line=BoxScoreLine(points=99 if did_play else 0),
        source=ESPN_SOURCE,
    )


def _espn(*boxes: PlayerBoxScore, status: GameStatus = GameStatus.FINAL) -> Any:
    game = ScheduledGame(
        provider_id="g1",
        start_time=LATE_START,
        status=status,
        home_team_id="PHI",
        away_team_id="DET",
        status_detail=None,
        source=ESPN_SOURCE,
    )
    result = ProviderResult(
        GameSummary(game=game, player_box_scores=boxes),
        quality_for_records(resource="game_summary", records=boxes, retrieved_at=NOW),
    )

    async def fetch(game_id: str) -> ProviderResult[GameSummary]:
        return result

    return fetch


def _reader(payload: dict[str, Any] | Exception, calls: list[tuple[str, int]] | None = None) -> Any:
    async def read(player_id: str, season: int) -> dict[str, Any]:
        if calls is not None:
            calls.append((player_id, season))
        if isinstance(payload, Exception):
            raise payload
        return payload

    return read


def _run(espn: Any, read: Any) -> ProviderResult[GameSummary]:
    return asyncio.run(SleeperScoredSummarySource(espn, read)("g1", PLAYERS))


def test_rostered_player_scores_from_sleeper_on_the_eastern_game_date() -> None:
    calls: list[tuple[str, int]] = []

    result = _run(_espn(_box("espn-embiid"), _box("other")), _reader({"3": [ROW]}, calls))

    by_player = {box.player_id: box for box in result.records.player_box_scores}
    assert by_player["espn-embiid"].line == BoxScoreLine(points=26, turnovers=4, technical_fouls=1)
    assert by_player["espn-embiid"].additional_sources[-1].provider == "sleeper"
    assert by_player["other"].line.points == 99
    assert calls == [("1511", 2026)]
    assert result.quality.state is DataQualityState.FRESH


def test_unfinished_game_does_not_read_sleeper() -> None:
    calls: list[tuple[str, int]] = []

    result = _run(_espn(_box("espn-embiid"), status=GameStatus.IN_PROGRESS), _reader({}, calls))

    assert calls == []
    assert result.records.player_box_scores[0].line.points == 99


def _assert_waiting(result: ProviderResult[GameSummary]) -> None:
    assert result.quality.state is DataQualityState.PARTIAL
    assert result.quality.warnings[0].startswith("sleeper_stats_unavailable")


def test_waits_when_sleeper_has_no_row() -> None:
    _assert_waiting(_run(_espn(_box("espn-embiid")), _reader({"3": []})))


def test_waits_when_a_player_who_played_has_empty_stats() -> None:
    payload = {"3": [{"date": "2026-11-04", "stats": {}}]}

    _assert_waiting(_run(_espn(_box("espn-embiid")), _reader(payload)))


def test_player_who_sat_out_with_empty_stats_is_not_held_up() -> None:
    payload = {"3": [{"date": "2026-11-04", "stats": {}}]}

    result = _run(_espn(_box("espn-embiid", did_play=False)), _reader(payload))

    assert result.quality.state is DataQualityState.FRESH
    assert result.records.player_box_scores[0].line == BoxScoreLine()


def test_waits_when_sleeper_request_or_payload_fails() -> None:
    _assert_waiting(
        _run(_espn(_box("espn-embiid")), _reader(ProviderHTTPError("Sleeper", 500, "stats")))
    )
    malformed = {"3": [{"date": "2026-11-04", "stats": {"pts": "many"}}]}
    _assert_waiting(_run(_espn(_box("espn-embiid")), _reader(malformed)))
