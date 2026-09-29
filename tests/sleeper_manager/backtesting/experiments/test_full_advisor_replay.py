"""End-to-end coverage for counterfactual weekly lineup and Lock-In replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.backtesting.replay.projection_surface import full_advisor_planning_cutoffs
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureError,
    ForecastRetrievalStatus,
    ForecastSource,
)
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import (
    BASE,
    _projection_surface,
    _team_week,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    SOURCE,
    failed_capture,
    successful_capture,
)


def _request(**team_week_overrides: object) -> FullAdvisorReplayRequest:
    """Build a small deterministic current/current replay request."""

    team_week = _team_week(**team_week_overrides)
    return FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(team_week),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )


def test_full_replay_builds_lineups_and_scores_a_legal_week() -> None:
    """Choose simulated starters, evaluate their games, and finish below the oracle."""

    first = run_full_advisor_replay(_request(p1_actual=30, p2_expected=8, p2_actual=8))
    second = run_full_advisor_replay(_request(p1_actual=30, p2_expected=8, p2_actual=8))

    assert first.to_dict() == second.to_dict()
    assert first.status == "success"
    assert first.plans
    assert first.lineup_traces
    assert first.started_player_games
    assert first.model_result.realized_score <= first.oracle_result.realized_score
    assert all(passed for _, passed in first.invariant_results)


def test_full_replay_ignores_bundle_projection_timestamps() -> None:
    """Use the explicit cutoff surface instead of bundle diagnostic projections."""

    request = _request(future_projection_available_as_of=BASE + timedelta(hours=10))

    assert run_full_advisor_replay(request).status == "success"


def test_full_replay_hashes_projection_surface_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse immutable surface identity across every replay planning cutoff."""

    request = _request()
    surface_type = type(request.projection_surface)
    fingerprint_property = surface_type.fingerprint
    assert fingerprint_property.fget is not None
    calls = 0

    def counted(surface: object) -> str:
        """Count full-payload hashes while preserving the stable identity."""

        nonlocal calls
        calls += 1
        return fingerprint_property.fget(surface)  # type: ignore[arg-type]

    monkeypatch.setattr(surface_type, "fingerprint", property(counted))

    assert run_full_advisor_replay(request).status == "success"
    assert calls == 1


def test_full_replay_rejects_recorded_surface_failure() -> None:
    """Fail closed when the exact first-cutoff projection could not be generated."""

    team_week = _team_week()
    first_cutoff = full_advisor_planning_cutoffs(team_week)[0]
    request = FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(
            team_week,
            fail_key=(first_cutoff, "p1", "g1"),
        ),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )

    with pytest.raises(FullAdvisorReplayError, match="projection_surface_failure"):
        run_full_advisor_replay(request)


def test_full_replay_preserves_a_starter_during_an_overlapping_game() -> None:
    """Keep the first game's starter eligible while planning the second tipoff."""

    team_week = _team_week(include_second_p1_game=False)
    overlapping_second = replace(
        team_week.games[1],
        start_time=BASE + timedelta(hours=1),
        final_time=BASE + timedelta(hours=3),
    )
    team_week = replace(team_week, games=(team_week.games[0], overlapping_second))
    request = FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(team_week),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )

    execution = run_full_advisor_replay(request)

    assert execution.status == "success"
    assert any(
        {player for _, player in trace.assignments} == {"p1", "p2"}
        for trace in execution.lineup_traces
    )


def test_full_replay_reads_forecasts_beside_each_plan_without_changing_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Call the cutoff reader once per planning decision and keep the projection in charge."""

    plain = run_full_advisor_replay(_request())
    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    archive.save_capture(
        successful_capture(
            persisted_at=BASE - timedelta(days=1),
            player_id="p1",
            points=12,
        )
    )
    archive.save_capture(failed_capture(persisted_at=BASE - timedelta(hours=3)))
    loads = 0
    original = archive.load_revision_at_cutoff

    def _count(source: ForecastSource, *, cutoff: object) -> object:
        nonlocal loads
        loads += 1
        return original(source, cutoff=cutoff)  # type: ignore[arg-type]

    archive.load_revision_at_cutoff = _count  # type: ignore[method-assign]
    request = _request()
    request = replace(request, forecast_archive=archive, forecast_source=SOURCE)
    team_week = request.team_week
    cutoffs = full_advisor_planning_cutoffs(team_week, request.planning_lead_time)

    execution = run_full_advisor_replay(request)

    assert execution.fingerprint == plain.fingerprint
    assert loads == len(cutoffs)
    assert len(request.forecast_reads) == len(cutoffs) * len(team_week.roster_player_ids)
    assert {read.player_id for read in request.forecast_reads} == {"p1", "p2"}
    assert all(
        read.status is ForecastRetrievalStatus.AVAILABLE
        and read.newer_attempt_receipt_id == "failed-1"
        for read in request.forecast_reads
        if read.player_id == "p1"
    )
    assert all(
        read.status is ForecastRetrievalStatus.MISSING and read.forecast is None
        for read in request.forecast_reads
        if read.player_id == "p2"
    )
    assert _request().forecast_reads == []


def test_full_replay_skips_cutoffs_before_the_first_forecast_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An archive that starts midweek cannot prevent an otherwise valid replay."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    request = _request()
    cutoffs = full_advisor_planning_cutoffs(request.team_week, request.planning_lead_time)
    archive.save_capture(failed_capture(persisted_at=cutoffs[0] + timedelta(minutes=1)))
    request = replace(request, forecast_archive=archive, forecast_source=SOURCE)

    execution = run_full_advisor_replay(request)

    assert execution.status == "success"
    assert len(request.forecast_reads) == (len(cutoffs) - 1) * len(
        request.team_week.roster_player_ids
    )
    assert {read.cutoff for read in request.forecast_reads} == set(cutoffs[1:])


def test_full_replay_replaces_forecast_reads_on_repeated_runs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A reused request exposes only the completed run's forecast reads."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    request = _request()
    first_cutoff = full_advisor_planning_cutoffs(request.team_week, request.planning_lead_time)[0]
    archive.save_capture(failed_capture(persisted_at=first_cutoff - timedelta(days=1)))
    request = replace(request, forecast_archive=archive, forecast_source=SOURCE)

    run_full_advisor_replay(request)
    first_reads = tuple(request.forecast_reads)
    run_full_advisor_replay(request)

    assert tuple(request.forecast_reads) == first_reads


def test_failed_replay_does_not_publish_partial_forecast_reads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A later archive failure leaves no reads from the incomplete replay."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    request = _request()
    cutoffs = full_advisor_planning_cutoffs(request.team_week, request.planning_lead_time)
    archive.save_capture(failed_capture(persisted_at=cutoffs[0] - timedelta(days=1)))
    request = replace(request, forecast_archive=archive, forecast_source=SOURCE)
    original = archive.load_revision_at_cutoff

    def fail_second_cutoff(source: ForecastSource, *, cutoff: object) -> object:
        """Allow one read, then simulate a corrupt later archive selection."""

        if cutoff == cutoffs[1]:
            raise ForecastCaptureError("corrupt revision")
        return original(source, cutoff=cutoff)  # type: ignore[arg-type]

    archive.load_revision_at_cutoff = fail_second_cutoff  # type: ignore[method-assign]

    with pytest.raises(FullAdvisorReplayError, match="corrupt revision"):
        run_full_advisor_replay(request)

    assert request.forecast_reads == []
