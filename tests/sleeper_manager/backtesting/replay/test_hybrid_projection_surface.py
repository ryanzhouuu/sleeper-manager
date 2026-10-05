"""Opening-week candidate sidecars execute unchanged full-advisor replay on both rosters."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.backtesting.replay.hybrid_projection_surface import (
    build_hybrid_projection_surfaces,
)
from sleeper_manager.backtesting.replay.projection_surface_artifact import (
    load_historical_projection_surface_artifact,
    write_historical_projection_surface_artifact,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import _team_week
from tests.sleeper_manager.projections.hybrid_support import TIPOFF, history
from tests.sleeper_manager.projections.test_hybrid_model import POLICY


def opening_week(roster: int = 1):  # type: ignore[no-untyped-def]
    original = _team_week()
    games = tuple(
        replace(
            game,
            start_time=TIPOFF + timedelta(hours=i * 24),
            final_time=TIPOFF + timedelta(hours=i * 24 + 3),
        )
        for i, game in enumerate(original.games)
    )
    player_games = tuple(
        replace(
            row,
            sleeper_id=f"r{roster}-{row.sleeper_id}",
            provider_player_id="target" if row.sleeper_id == "p1" else "donor-0",
            fantasy_team_id=roster,
            actual_score=float(row.actual_score),
            projection=replace(row.projection, player_id=f"r{roster}-{row.sleeper_id}"),
        )
        for row in original.player_games
    )
    return replace(
        original,
        manifest_id=f"manifest-{roster}",
        roster_id=roster,
        games=games,
        player_games=player_games,
        roster_player_ids=tuple(f"r{roster}-{p}" for p in original.roster_player_ids),
        observed_starter_ids=tuple(f"r{roster}-{p}" for p in original.observed_starter_ids),
    )


def request(week, surface):  # type: ignore[no-untyped-def]
    return FullAdvisorReplayRequest(
        team_week=week,
        projection_surface=surface,
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )


def test_complete_both_roster_surfaces_round_trip_and_run_full_replay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    weeks = (opening_week(1), opening_week(2))
    provider = HybridProjectionProvider(history())
    built = build_hybrid_projection_surfaces(weeks, provider, scoring_policy=POLICY)
    assert len(built.surfaces) == 2
    assert len(built.attempts) == sum(len(surface.entries) for surface in built.surfaces)
    for week, surface in zip(weeks, built.surfaces, strict=True):
        assert len(surface.entries) == 6
        assert all(entry.projection is not None for entry in surface.entries)
        path = tmp_path / f"{week.roster_id}.json"
        write_historical_projection_surface_artifact(path, surface)
        decoded = load_historical_projection_surface_artifact(path, team_week=week)
        assert decoded == surface
        execution = run_full_advisor_replay(request(week, decoded))
        assert execution.status == "success"
        assert all(passed for _, passed in execution.invariant_results)
        assert execution.to_dict() == run_full_advisor_replay(request(week, decoded)).to_dict()


def test_blocked_projection_stays_in_surface_and_replay_fails_closed() -> None:
    week = opening_week()
    provider = HybridProjectionProvider(replace(history(), opportunities=()))
    built = build_hybrid_projection_surfaces((week,), provider, scoring_policy=POLICY)
    surface = built.surfaces[0]
    assert len(surface.entries) == 6
    assert all(
        entry.failure_reason == "missing_pooled_participation_opportunities"
        for entry in surface.entries
    )
    with pytest.raises(FullAdvisorReplayError, match="projection_surface_failure"):
        run_full_advisor_replay(request(week, surface))


def test_pairing_unrelated_weeks_is_rejected() -> None:
    with pytest.raises(HybridProjectionError, match="scope_mismatch"):
        build_hybrid_projection_surfaces(
            (opening_week(1), replace(opening_week(2), week=2)),
            HybridProjectionProvider(history()),
            scoring_policy=POLICY,
        )


def test_archived_external_forecast_drives_the_candidate_full_replay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
    from tests.sleeper_manager.projections.hybrid_support import CUTOFF
    from tests.sleeper_manager.projections.test_hybrid_model import batch, forecast
    from tests.sleeper_manager.projections.test_hybrid_provider import capture

    weeks = (opening_week(1), opening_week(2))
    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.sqlite")
    archive.initialize()
    player = replace(forecast(batch().pools.pool("target").means).forecast, player_id="r1-p1")
    archive.save_capture(capture("opening", CUTOFF, player=player))
    built = build_hybrid_projection_surfaces(
        weeks, HybridProjectionProvider(history(), archive=archive), scoring_policy=POLICY
    )
    assert any(
        attempt.source == "external" and attempt.target.sleeper_player_id == "r1-p1"
        for attempt in built.attempts
    )
    assert any(attempt.external_rejection == "stale_forecast_receipt" for attempt in built.attempts)
    assert all(
        run_full_advisor_replay(request(week, surface)).status == "success"
        for week, surface in zip(weeks, built.surfaces, strict=True)
    )
