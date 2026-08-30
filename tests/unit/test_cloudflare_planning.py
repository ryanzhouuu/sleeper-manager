import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from sleeper_manager.cloudflare.planning import collect_cloudflare_planning_inputs
from sleeper_manager.config import ManagerPolicy
from sleeper_manager.decisions.weekly_plan import build_weekly_plan
from sleeper_manager.domain.planning import PlanStatus
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import ProjectionObservationRecord
from sleeper_manager.projections.live_baseline import ProjectionHistoryError
from sleeper_manager.workflows.planning_inputs import build_live_team_week_state

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 1, 5, 14, tzinfo=UTC)


class Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class FixtureFetch:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, url: str) -> Response:
        path = urlparse(url).path
        self.paths.append(path)
        fixture = self._fixture_for(path)
        return Response(json.loads(fixture.read_text()))

    @staticmethod
    def _fixture_for(path: str) -> Path:
        routes = {
            "/v1/league/league-current": "sleeper/current_league.json",
            "/v1/league/league-current/users": "sleeper/users.json",
            "/v1/league/league-current/rosters": "sleeper/rosters.json",
            "/v1/league/league-current/transactions/1": "sleeper/transactions.json",
            "/v1/state/nba": "sleeper/state.json",
            "/v1/players/nba": "planning_inputs/sleeper_players_catalog.json",
            "/apis/site/v2/sports/basketball/nba/teams/DEN/roster": (
                "planning_inputs/espn_team_roster.json"
            ),
            "/apis/site/v2/sports/basketball/nba/teams/team-home/schedule": (
                "planning_inputs/espn_team_schedule.json"
            ),
            "/apis/site/v2/sports/basketball/nba/injuries": ("planning_inputs/espn_injuries.json"),
        }
        return FIXTURES / routes[path]


def test_cloudflare_assembly_fails_closed_without_projection_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise():  # type: ignore[no-untyped-def]
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        return await collect_cloudflare_planning_inputs(
            SimpleNamespace(
                SLEEPER_LEAGUE_ID="league-current",
                SLEEPER_USER_ID="user-manager",
            ),
            FixtureFetch(),
            repository=repository,
            policy=default_runtime_policy(history_version="missing-history-v1"),
            scheduled_at=NOW,
            clock=lambda: NOW,
        )

    with pytest.raises(ProjectionHistoryError, match="no observations"):
        asyncio.run(exercise())


def test_cloudflare_assembly_refreshes_live_evidence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise():  # type: ignore[no-untyped-def]
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
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
        fetch = FixtureFetch()
        assembly = await collect_cloudflare_planning_inputs(
            SimpleNamespace(
                SLEEPER_LEAGUE_ID="league-current",
                SLEEPER_USER_ID="user-manager",
            ),
            fetch,
            repository=repository,
            policy=default_runtime_policy(history_version="history-v1"),
            scheduled_at=NOW,
            clock=lambda: NOW,
        )
        return assembly, fetch

    assembly, fetch = asyncio.run(exercise())
    state = build_live_team_week_state(
        assembly.evidence.inputs,
        decision_time=assembly.evidence.decision_time,
    )

    assert assembly.evidence.inputs.runtime_policy_version == ManagerPolicy().version
    assert assembly.local_timezone.key == "America/Chicago"
    assert assembly.player_names["player-1"] == "Fixture Point Guard"
    plan = build_weekly_plan(state)
    assert plan.status is PlanStatus.NO_ACTION
    assert plan.blocking_reasons == ()
    assert state.projection_model_version.startswith("projection-baseline-v1-")
    freshness = {source.source: source.version for source in state.freshness.sources}
    assert freshness["projection-history"] == "history-v1"
    assert freshness["sleeper-player-catalog"].startswith("sleeper-catalog-v1-")
    assert "nba:team-roster:DEN" in freshness
    assert "nba:team-schedule:team-home" in freshness
    assert "nba:injuries" in freshness
    assert "/v1/league/league-current/rosters" in fetch.paths
