"""Live planning collection, identity, and freshness assembly."""

from datetime import datetime, timedelta

import pytest

from sleeper_manager.domain.nba import (
    ProviderPlayer,
)
from sleeper_manager.domain.planning import (
    PlanningReasonCode,
)
from sleeper_manager.workflows.planning_collection import (
    PlanningCollectionError,
)
from sleeper_manager.workflows.planning_inputs import (
    LiveProjectionResult,
    build_live_team_week_state,
)
from tests.sleeper_manager.workflows.planning_collection_support import (
    NOW,
    WINDOW_END,
    _catalog,
    _collect,
    _FakeNBA,
    _game,
    _nba,
    _provider_source,
    _RecordingProjections,
    _StaticCatalog,
    asyncio_run,
)


def test_collect_and_assemble_complete_live_state() -> None:
    projections = _RecordingProjections()

    async def run() -> None:
        evidence = await _collect(_nba(), projections)
        state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
        return evidence, state

    evidence, state = asyncio_run(run())

    assert evidence.inputs.week_window.week == 1
    assert not state.is_blocked
    assert len(state.opportunities) == 4
    assert all(opportunity.projection is not None for opportunity in state.opportunities)
    first = next(opportunity for opportunity in state.opportunities if opportunity.game_id == "g1")
    assert first.availability_status == "questionable"
    assert first.eligible_slot_indices == (0, 1)
    resources = {source.source for source in state.freshness.sources}
    assert "nba:team-schedule:12" in resources and "nba:injuries" in resources
    assert [target.game_id for target in projections.targets] == ["g1", "g2", "g1", "g2"]
    assert [target.sleeper_player_id for target in projections.targets][:2] == ["p1", "p1"]


def test_roster_freshness_sources_are_scoped_per_team() -> None:
    catalog = {
        "p1": {
            "full_name": "Point Guard One",
            "team": "CHI",
            "espn_id": 401,
            "fantasy_positions": ["PG"],
        },
        "p2": {
            "full_name": "Center Two",
            "team": "BOS",
            "espn_id": 402,
            "fantasy_positions": ["C"],
        },
    }
    nba = _FakeNBA()
    nba.rosters["CHI"] = (
        ProviderPlayer("401", "Point Guard One", "12", "chi", True, _provider_source()),
    )
    nba.rosters["BOS"] = (
        ProviderPlayer("402", "Center Two", "14", "bos", True, _provider_source()),
    )
    projections = _RecordingProjections()

    async def run() -> object:
        return await _collect(
            nba,
            projections,
            catalog_source=_StaticCatalog(catalog),
        )

    evidence = asyncio_run(run())
    roster_resources = [report.resource for report in evidence.inputs.identity_quality_reports]

    assert roster_resources == ["nba:team-roster:BOS", "nba:team-roster:CHI"]
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert {
        source.source
        for source in state.freshness.sources
        if source.source.startswith("nba:team-roster:")
    } == set(roster_resources)


def test_schedule_provider_failure_blocks_planning() -> None:
    nba = _nba()
    nba.schedule_failures.add("12")

    async def run() -> None:
        return await _collect(nba, _RecordingProjections())

    evidence = asyncio_run(run())

    assert any("team-schedule:12" in warning for warning in evidence.warnings)
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons
    assert state.opportunities == ()


def test_injury_provider_failure_blocks_planning() -> None:
    nba = _nba()
    nba.injury_failure = True

    async def run() -> None:
        return await _collect(nba, _RecordingProjections())

    evidence = asyncio_run(run())
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons


def test_missing_catalog_player_stays_unresolved() -> None:
    catalog = _catalog()
    del catalog["p2"]

    async def run() -> None:
        return await _collect(
            _nba(), _RecordingProjections(), catalog_source=_StaticCatalog(catalog)
        )

    evidence = asyncio_run(run())

    assert any("p2" in warning for warning in evidence.warnings)
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY in state.blocking_reasons


def test_out_of_window_games_are_not_projected() -> None:
    nba = _nba()
    nba.schedules["12"] = (
        _game("g1"),
        _game("g9", start=WINDOW_END + timedelta(days=1)),
    )
    projections = _RecordingProjections()

    async def run() -> None:
        return await _collect(nba, projections)

    evidence = asyncio_run(run())

    assert {target.game_id for target in projections.targets} == {"g1"}
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert {opportunity.game_id for opportunity in state.opportunities} == {"g1"}


def test_mapping_override_resolves_identity() -> None:
    catalog = {
        "p1": {"full_name": "Renamed Guard", "team": "CHI", "fantasy_positions": ["PG"]},
        "p2": {
            "full_name": "Center Two",
            "team": "CHI",
            "espn_id": 402,
            "fantasy_positions": ["C"],
        },
    }

    async def run() -> None:
        return await _collect(
            _nba(),
            _RecordingProjections(),
            catalog_source=_StaticCatalog(catalog),
            mapping_overrides={"p1": "401"},
        )

    evidence = asyncio_run(run())

    identities = {item.sleeper_player_id: item for item in evidence.inputs.identities}
    assert identities["p1"].provider_player_id == "401"
    assert identities["p1"].method == "explicit_override"


def test_naive_clock_output_is_rejected() -> None:
    async def run() -> None:
        await _collect(_nba(), _RecordingProjections(), clock=lambda: datetime(2026, 1, 7, 18))

    with pytest.raises(PlanningCollectionError, match="timezone-aware"):
        asyncio_run(run())


def test_eligibility_is_stamped_with_catalog_retrieval_time() -> None:
    ticks = iter([NOW - timedelta(hours=1), NOW, NOW, NOW, NOW])

    async def run() -> None:
        return await _collect(_nba(), _RecordingProjections(), clock=lambda: next(ticks))

    evidence = asyncio_run(run())
    eligibility = {item.sleeper_player_id: item for item in evidence.inputs.player_eligibility}
    assert eligibility["p1"].available_as_of == NOW - timedelta(hours=1)
    assert evidence.decision_time == NOW
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert not state.is_blocked


def test_result_type_shape_is_preserved() -> None:
    result = LiveProjectionResult(
        sleeper_player_id="p1",
        game_id="g1",
        snapshot=None,
        missing_reason=PlanningReasonCode.MISSING_PROJECTION,
    )
    assert result.snapshot is None
