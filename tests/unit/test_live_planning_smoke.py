"""Opt-in read-only smoke against current Sleeper and ESPN providers.

Run with FANTASY_MANAGER_LIVE_SMOKE=1 and configured SLEEPER_LEAGUE_ID /
SLEEPER_USER_ID. Never invoked by the default suite. Projection history
binding lands with Tasks 5.3-5.4, so remaining games legitimately carry
missing-projection reasons; the smoke proves the live adapter pipeline
builds a complete, infrastructure-clean shared state read-only.
"""

import asyncio
import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sleeper_manager.config import Settings
from sleeper_manager.integrations.nba.cached_provider import CachedNBAProvider
from sleeper_manager.integrations.nba.espn import ESPNClient
from sleeper_manager.integrations.sleeper.client import SleeperClient
from sleeper_manager.integrations.sleeper.sync import LeagueSynchronizationService
from sleeper_manager.persistence.nba_cache import SQLiteNBADataCache
from sleeper_manager.projections.live_baseline import (
    DirectBaselineProjectionProvider,
    HistoricalFeatureSlice,
)
from sleeper_manager.workflows.planning_collection import (
    collect_live_planning_inputs,
)
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    PlanningFreshnessPolicy,
    PlanningReasonCode,
    build_live_team_week_state,
)


class _SyncedProfileSource:
    def __init__(self, service: LeagueSynchronizationService, settings: Settings) -> None:
        self._service = service
        self._settings = settings
        self._profile = None

    async def fetch(self):  # type: ignore[no-untyped-def]
        if self._profile is None:
            result = await self._service.sync(
                league_id=self._settings.sleeper_league_id,
                user_id=self._settings.sleeper_user_id,
            )
            self._profile = result.profile
        return self._profile


class _EmptyProjectionHistory:
    def load(self, target, *, before: datetime) -> HistoricalFeatureSlice:  # noqa: ANN001
        return HistoricalFeatureSlice(
            dataset_version="smoke-none",
            feature_schema_version="1",
            source_versions=(),
            rows=(),
        )


class _NoAcknowledgements:
    """Replaced by the repository query in Task 5.2."""

    async def load(self, league_id: str, week: int, *, as_of: datetime) -> tuple:  # noqa: ANN401
        return ()


def _week_window_containing(profile) -> FantasyWeekWindow:  # noqa: ANN001
    eastern = ZoneInfo("America/New_York")
    local_day = datetime.now(UTC).astimezone(eastern).date()
    monday = local_day - timedelta(days=local_day.weekday())
    starts_at = datetime.combine(monday, time.min, tzinfo=eastern)
    return FantasyWeekWindow(
        week=profile.fantasy_week.week,
        starts_at=starts_at.astimezone(UTC),
        ends_at=(starts_at + timedelta(days=7)).astimezone(UTC),
    )


@pytest.mark.skipif(
    os.environ.get("FANTASY_MANAGER_LIVE_SMOKE") != "1",
    reason="live smoke is opt-in via FANTASY_MANAGER_LIVE_SMOKE=1",
)
def test_live_providers_build_a_shared_state_read_only(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.sleeper_configured, (
        "SLEEPER_LEAGUE_ID and SLEEPER_USER_ID must be configured for the live smoke"
    )
    mapping_overrides = settings.load_manager_policy().players.mapping_overrides

    async def run() -> None:
        async with SleeperClient() as sleeper, ESPNClient() as espn:
            service = LeagueSynchronizationService(sleeper)
            profile_source = _SyncedProfileSource(service, settings)
            profile = await profile_source.fetch()
            cache = SQLiteNBADataCache(tmp_path / "smoke-cache.db")
            cache.initialize()

            evidence = await collect_live_planning_inputs(
                profile_source=profile_source,
                catalog_source=sleeper,
                nba=CachedNBAProvider(espn, cache),
                projection_provider=DirectBaselineProjectionProvider(_EmptyProjectionHistory()),
                week_window=_week_window_containing(profile),
                freshness_policy=PlanningFreshnessPolicy(
                    max_sleeper_age=timedelta(minutes=30),
                    max_nba_schedule_age=timedelta(hours=12),
                    max_availability_age=timedelta(hours=12),
                ),
                acknowledgement_source=_NoAcknowledgements(),
                mapping_overrides=mapping_overrides,
            )

        state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)

        print("decision:", evidence.decision_time, "week:", state.week)
        print("blocked:", [reason.value for reason in state.blocking_reasons])
        print("warnings:", [reason.value for reason in state.warnings])
        print("opportunities:", len(state.opportunities))
        for warning in evidence.warnings:
            print("collection warning:", warning)

        assert state.week == evidence.inputs.league_profile.fantasy_week.week
        assert state.roster_player_ids
        # Freshly collected evidence must never be newer than its decision time.
        for source in state.freshness.sources:
            assert source.available_as_of <= evidence.decision_time
        # Infrastructure failures would show up as stale-state blocks; a healthy
        # run may still block on data-quality grounds (e.g. unresolved identity).
        infrastructure_blocks = {
            PlanningReasonCode.STALE_SLEEPER_STATE,
            PlanningReasonCode.STALE_NBA_STATE,
        }
        assert not infrastructure_blocks.intersection(state.blocking_reasons)

    asyncio.run(run())
