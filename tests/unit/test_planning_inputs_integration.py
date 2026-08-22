import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx

from sleeper_manager.domain.nba import ProviderResult, ScheduledGame
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.integrations.nba.cached_provider import CachedNBAProvider
from sleeper_manager.integrations.nba.espn import (
    parse_injuries,
    parse_team_roster,
    parse_team_schedule,
)
from sleeper_manager.integrations.sleeper.client import SleeperClient
from sleeper_manager.integrations.sleeper.sync import LeagueSynchronizationService
from sleeper_manager.persistence.nba_cache import SQLiteNBADataCache
from sleeper_manager.projections.live_baseline import (
    DirectBaselineProjectionProvider,
    HistoricalFeatureSlice,
)
from sleeper_manager.workflows.planning_collection import collect_live_planning_inputs
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    PlanningFreshnessPolicy,
    build_live_team_week_state,
)

SLEEPER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "sleeper"
PLANNING_FIXTURES = Path(__file__).parents[1] / "fixtures" / "planning_inputs"

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
RETRIEVED_AT = NOW - timedelta(minutes=5)
WINDOW_START = datetime(2026, 1, 5, 5, tzinfo=UTC)
WINDOW_END = datetime(2026, 1, 12, 5, tzinfo=UTC)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _route_sleeper() -> None:
    league_id = "league-current"
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}").mock(
        return_value=httpx.Response(200, json=_load(SLEEPER_FIXTURES / "current_league.json"))
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/users").mock(
        return_value=httpx.Response(200, json=_load(SLEEPER_FIXTURES / "users.json"))
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters").mock(
        return_value=httpx.Response(200, json=_load(SLEEPER_FIXTURES / "rosters.json"))
    )
    respx.get("https://api.sleeper.app/v1/state/nba").mock(
        return_value=httpx.Response(200, json=_load(SLEEPER_FIXTURES / "state.json"))
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/transactions/1").mock(
        return_value=httpx.Response(200, json=_load(SLEEPER_FIXTURES / "transactions.json"))
    )


class _FixtureNBAProvider:
    """Parses recorded sanitized ESPN payloads through the real parser stack."""

    async def scoreboard(self, game_date: object) -> ProviderResult[tuple[ScheduledGame, ...]]:
        raise NotImplementedError

    async def game_summary(self, game_id: str) -> ProviderResult[Any]:
        raise NotImplementedError

    async def team_roster(self, team_id: str):
        assert team_id == "DEN"
        return parse_team_roster(
            _load(PLANNING_FIXTURES / "espn_team_roster.json"),
            team_id="team-home",
            retrieved_at=RETRIEVED_AT,
        )

    async def team_schedule(self, team_id: str, season: int):
        assert season == 2026
        return parse_team_schedule(
            _load(PLANNING_FIXTURES / "espn_team_schedule.json"),
            retrieved_at=RETRIEVED_AT,
        )

    async def injuries(self):
        return parse_injuries(
            _load(PLANNING_FIXTURES / "espn_injuries.json"),
            retrieved_at=RETRIEVED_AT,
        )


class _ExplodingNBA:
    def __getattr__(self, name):  # noqa: ANN204
        def _fail(*_: object, **__: object) -> None:
            raise AssertionError(f"{name} must be served from cache")

        return _fail


class _StaticHistory:
    """Two finalized prior games per provider player, keyed by ESPN ID."""

    def load(self, target, *, before: datetime) -> HistoricalFeatureSlice:  # noqa: ANN001
        rows = []
        for offset_days in (3, 1):
            start = before - timedelta(days=offset_days + 1)
            rows.append(_history_row(target.provider_player_id or "", start))
        return HistoricalFeatureSlice(
            dataset_version="features-v1",
            feature_schema_version="1",
            source_versions=(),
            rows=tuple(rows),
        )


def _history_row(player_id: str, game_start: datetime):
    from sleeper_manager.domain.nba import AvailabilityStatus
    from sleeper_manager.domain.scoring import BoxScoreLine
    from sleeper_manager.integrations.nba.historical_feature_models import (
        AvailabilityObservation,
        HistoricalFeatureRow,
    )

    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=game_start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
        game_id=f"{player_id}-{game_start.date().isoformat()}",
        game_start=game_start,
        outcome_finalized_at=game_start + timedelta(hours=2),
        team_id="DEN",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
        is_home=True,
        days_rest=2,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.UNKNOWN,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=30,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=20),
        target_line_points=20,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(),
    )


@respx.mock
def test_recorded_fixtures_build_state_and_cache_serves_repeat_runs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _route_sleeper()
    cache = SQLiteNBADataCache(tmp_path / "cache.db")
    cache.initialize()

    async def run() -> tuple[object, object]:
        async with SleeperClient() as sleeper:
            service = LeagueSynchronizationService(sleeper, clock=lambda: NOW)
            profile_source = _ServiceProfileSource(service)

            first = await collect_live_planning_inputs(
                profile_source=profile_source,
                catalog_source=_CatalogSource(),
                nba=CachedNBAProvider(_FixtureNBAProvider(), cache, clock=lambda: NOW),
                projection_provider=DirectBaselineProjectionProvider(
                    _StaticHistory(),
                    config=None,
                ),
                week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
                freshness_policy=_freshness_policy(),
                clock=lambda: NOW,
            )
            state_first = build_live_team_week_state(
                first.inputs, decision_time=first.decision_time
            )

            second = await collect_live_planning_inputs(
                profile_source=profile_source,
                catalog_source=_CatalogSource(),
                nba=CachedNBAProvider(_ExplodingNBA(), cache, clock=lambda: NOW),
                projection_provider=DirectBaselineProjectionProvider(_StaticHistory()),
                week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
                freshness_policy=_freshness_policy(),
                clock=lambda: NOW,
            )
            state_second = build_live_team_week_state(
                second.inputs, decision_time=second.decision_time
            )
            return state_first, state_second

    state_first, state_second = asyncio.run(run())

    for state in (state_first, state_second):
        assert not state.is_blocked
        assert state.week == 1
        assert len(state.opportunities) == 6
        assert {opportunity.game_id for opportunity in state.opportunities} == {
            "game-tuesday",
            "game-friday",
        }
        statuses = {
            (opportunity.sleeper_player_id, opportunity.game_id): opportunity.availability_status
            for opportunity in state.opportunities
        }
        assert statuses[("player-1", "game-friday")] == "questionable"
        assert statuses[("player-3", "game-tuesday")] == "out"
        # The recorded decision falls after game-tuesday's tipoff, so only the
        # remaining Friday game receives a pregame projection.
        for opportunity in state.opportunities:
            if opportunity.game_id == "game-friday":
                assert opportunity.projection is not None
                assert opportunity.missing_projection_reason is None
            else:
                assert opportunity.projection is None
                assert (
                    opportunity.missing_projection_reason is PlanningReasonCode.MISSING_PROJECTION
                )
    assert PlanningReasonCode.PROJECTION_AFTER_DECISION not in state_second.blocking_reasons


class _ServiceProfileSource:
    def __init__(self, service: LeagueSynchronizationService) -> None:
        self._service = service
        self._profile = None

    async def fetch(self):  # type: ignore[no-untyped-def]
        if self._profile is None:
            result = await self._service.sync(league_id="league-current", user_id="user-manager")
            self._profile = result.profile
        return self._profile


class _CatalogSource:
    async def players(self):  # type: ignore[no-untyped-def]
        return _load(PLANNING_FIXTURES / "sleeper_players_catalog.json")


def _freshness_policy() -> PlanningFreshnessPolicy:
    return PlanningFreshnessPolicy(
        max_sleeper_age=timedelta(hours=6),
        max_nba_schedule_age=timedelta(hours=12),
        max_availability_age=timedelta(hours=12),
    )
