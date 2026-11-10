import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sleeper_manager.cloudflare.home_cache import read_home_cache
from sleeper_manager.cloudflare.opponent_outlook import (
    OpponentGame,
    decode_outlook,
    load_opponent_outlook,
    outlook_cache_key,
    outlook_payload,
    refresh_opponent_outlook,
)
from sleeper_manager.cloudflare.planning import (
    CloudflarePlanningAssembly,
    collect_cloudflare_planning_inputs,
)
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import ProjectionObservationRecord
from tests.sleeper_manager.cloudflare.test_cloudflare_planning import NOW, FixtureFetch


async def _assembly(repository: AsyncSQLiteStateRepository) -> CloudflarePlanningAssembly:
    await repository.initialize()
    await repository.save_projection_observations(
        (
            ProjectionObservationRecord(
                history_version="history-v1",
                player_id="401",
                game_id="prior-game",
                game_start=NOW - timedelta(days=2),
                outcome_finalized_at=NOW - timedelta(days=1),
                minutes=30,
                started=True,
                did_not_play=False,
                box_score_json='{"points":20}',
                source_version="espn-v1",
            ),
        )
    )
    return await collect_cloudflare_planning_inputs(
        SimpleNamespace(SLEEPER_LEAGUE_ID="league-current", SLEEPER_USER_ID="user-manager"),
        FixtureFetch(),
        repository=repository,
        policy=default_runtime_policy(history_version="history-v1"),
        scheduled_at=NOW,
        clock=lambda: NOW,
    )


def test_opponent_evidence_belongs_to_the_paired_roster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise():  # type: ignore[no-untyped-def]
        assembly = await _assembly(AsyncSQLiteStateRepository(tmp_path / "state.db"))
        assert assembly.collect_opponent is not None
        return await assembly.collect_opponent()

    evidence = asyncio.run(exercise())

    assert evidence.inputs.league_profile.manager_roster_id == 2
    assert evidence.inputs.acknowledgements == ()
    assert outlook_payload(evidence) == {
        "roster_id": 2,
        "projected_at": NOW.isoformat(),
        "players": {"player-4": None, "player-5": None},
    }


def test_outlook_payload_keeps_each_game_in_start_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assembly = asyncio.run(_assembly(AsyncSQLiteStateRepository(tmp_path / "state.db")))

    payload = outlook_payload(assembly.evidence)

    assert payload is not None
    games = payload["players"]["player-1"]  # type: ignore[index]
    assert [game["start"] for game in games] == [
        "2026-01-06T00:00:00+00:00",
        "2026-01-09T00:00:00+00:00",
    ]
    assert all(game["outcomes"] == [20.0] * 20 for game in games)


def test_outlook_payload_marks_started_games_and_unprojected_players(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assembly = asyncio.run(_assembly(AsyncSQLiteStateRepository(tmp_path / "state.db")))
    evidence = assembly.evidence
    later = replace(evidence, decision_time=datetime(2026, 1, 7, tzinfo=UTC))
    unprojected = replace(
        evidence,
        inputs=replace(
            evidence.inputs,
            projections=tuple(
                row for row in evidence.inputs.projections if row.sleeper_player_id != "player-2"
            ),
        ),
    )

    started = outlook_payload(later)
    missing = outlook_payload(unprojected)

    assert started is not None and missing is not None
    assert started["players"]["player-1"][0]["outcomes"] is None  # type: ignore[index]
    assert started["players"]["player-1"][1]["outcomes"] == [20.0] * 20  # type: ignore[index]
    assert missing["players"]["player-2"] is None  # type: ignore[index]


def test_decode_outlook_reads_its_payload_and_rejects_others() -> None:
    payload = {
        "roster_id": 2,
        "projected_at": NOW.isoformat(),
        "players": {
            "player-4": [{"start": NOW.isoformat(), "outcomes": [10, 12.5]}],
            "player-5": None,
        },
    }

    outlook = decode_outlook(payload)

    assert outlook is not None
    assert outlook.roster_id == 2
    assert outlook.players == {"player-4": (OpponentGame(NOW, (10.0, 12.5)),), "player-5": None}
    assert decode_outlook({**payload, "roster_id": "2"}) is None
    assert decode_outlook({**payload, "players": {"p": [{"start": "2026-01-05T14:00:00"}]}}) is None
    assert (
        decode_outlook({**payload, "players": {"p": [{"start": NOW.isoformat(), "outcomes": []}]}})
        is None
    )


def test_refresh_projects_the_opponent_once_per_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    calls = []

    async def exercise():  # type: ignore[no-untyped-def]
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        assembly = await _assembly(repository)
        assert assembly.collect_opponent is not None
        collect_opponent = assembly.collect_opponent

        async def counted():  # type: ignore[no-untyped-def]
            calls.append(1)
            return await collect_opponent()

        counting = replace(assembly, collect_opponent=counted)
        await refresh_opponent_outlook(repository, counting, now=NOW)
        await refresh_opponent_outlook(repository, counting, now=NOW + timedelta(hours=1))
        return await load_opponent_outlook(repository, "league-current", 1, now=NOW)

    outlook = asyncio.run(exercise())

    assert calls == [1]
    assert outlook is not None and outlook.roster_id == 2


def test_refresh_failure_leaves_no_outlook(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    async def exercise():  # type: ignore[no-untyped-def]
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        assembly = await _assembly(repository)

        async def failing():  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

        await refresh_opponent_outlook(
            repository, replace(assembly, collect_opponent=failing), now=NOW
        )
        return await read_home_cache(repository, outlook_cache_key("league-current", 1), NOW)

    assert asyncio.run(exercise()) is None
    assert "Opponent outlook failed: RuntimeError" in capsys.readouterr().err
