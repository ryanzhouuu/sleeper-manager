"""Batch collection preserves ordering and applies factual absence constraints to advice inputs."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_projections import project_live_targets
from tests.sleeper_manager.workflows.planning_collection_support import (
    NOW,
    _collect,
    _nba,
    _RecordingProjections,
)
from tests.sleeper_manager.workflows.test_game_availability import evidence as official_evidence


class BatchProvider:
    def __init__(self, *, wrong_key: bool = False, wrong_count: bool = False) -> None:
        self.calls = 0
        self.wrong_key, self.wrong_count = wrong_key, wrong_count

    async def project_live_batch(self, targets, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        values = tuple(
            _RecordingProjections().project(
                target,
                scoring_policy=kwargs["scoring_policy"],
                decision_time=kwargs["decision_time"],
            )
            for target in targets
        )
        if self.wrong_count:
            return ()
        if self.wrong_key:
            return (replace(values[0], game_id="wrong"), *values[1:])
        return values


def test_collector_uses_one_batch_for_all_player_games() -> None:
    provider = BatchProvider()
    evidence = asyncio.run(_collect(_nba(), provider))
    assert provider.calls == 1
    assert len(evidence.inputs.projections) == 4


@pytest.mark.parametrize("kind", ["count", "key"])
def test_invalid_batch_cannot_attach_predictions_to_other_games(kind: str) -> None:
    provider = BatchProvider(wrong_count=kind == "count", wrong_key=kind == "key")
    with pytest.raises(ValueError, match="batch"):
        asyncio.run(_collect(_nba(), provider))


def project(
    *, kind: str = "out", age: int = 0, other_game: bool = False, missing: bool = False
) -> tuple[ProjectionSnapshot | None, ...]:
    targets = (
        LiveProjectionTarget("p1", "g1", NOW + timedelta(hours=1), "401"),
        LiveProjectionTarget("p1", "g2", NOW + timedelta(days=1), "401"),
    )
    report = GameAvailability(
        "other" if other_game else "g1",
        NOW - timedelta(minutes=age),
        AvailabilityStatus.OUT if kind == "out" else AvailabilityStatus.QUESTIONABLE,
        AvailabilityObservation.REPORTED,
        "official:hash",
        confirmed_absence=kind == "out",
    )

    class Provider:
        def project(self, target, **kwargs):  # type: ignore[no-untyped-def]
            return None if missing else _RecordingProjections().project(target, **kwargs)

    results = asyncio.run(
        project_live_targets(
            targets,
            projection_provider=Provider(),
            scoring_policy=ScoringPolicy(points=1),
            decision_time=NOW,
            availability={("p1", "g1"): report},
            max_availability_age=timedelta(minutes=15),
        )
    )
    return tuple(row.snapshot for row in results)


def test_confirmed_absence_changes_only_the_bound_games_distribution() -> None:
    first, later = project()
    assert first is not None and first.distribution.expected_value == 0
    assert first.distribution.weighted_observations == ((0.0, 1.0),)
    assert later is not None and later.distribution.expected_value == 10
    assert first.reasons[0].code == "confirmed_game_absence"


def test_confirmed_absence_does_not_need_a_warmed_up_baseline() -> None:
    first, later = project(missing=True)
    assert first is not None and first.distribution.expected_value == 0
    assert later is None


@pytest.mark.parametrize("kwargs", [{"age": 16}, {"age": -1}, {"other_game": True}])
def test_stale_future_or_wrong_game_reports_cannot_zero_advice(kwargs) -> None:  # type: ignore[no-untyped-def]
    first, _ = project(**kwargs)
    assert first is not None and first.distribution.expected_value == 10


def test_questionable_status_preserves_provisional_value_and_visible_warning() -> None:
    first, _ = project(kind="questionable")
    assert first is not None and first.distribution.expected_value == 10
    assert first.reasons[-1].code == "availability_uncertainty"


def test_official_report_reaches_collected_projections_and_retained_evidence() -> None:
    _, _, report = official_evidence()

    class Official:
        async def report(self, cutoff):  # type: ignore[no-untyped-def]
            return report

    nba = _nba()
    first_game, second_game = nba.schedules["12"]
    nba.schedules["12"] = (
        first_game,
        replace(second_game, start_time=second_game.start_time + timedelta(days=2)),
    )
    collected = asyncio.run(
        _collect(nba, _RecordingProjections(), official_availability_source=Official())
    )
    first = next(
        row
        for row in collected.inputs.projections
        if row.sleeper_player_id == "p1" and row.game_id == "g1"
    )
    assert first.snapshot.distribution.expected_value == 0
    assert any(
        player == "p1" and row.confirmed_absence for player, row in collected.game_availability
    )
    assert len(collected.projection_targets) == 4
    later = next(
        row
        for row in collected.inputs.projections
        if row.sleeper_player_id == "p1" and row.game_id == "g2"
    )
    assert later.snapshot.distribution.expected_value == 10


def test_official_provider_failure_is_retained_without_breaking_control() -> None:
    class Official:
        async def report(self, cutoff):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    collected = asyncio.run(
        _collect(_nba(), _RecordingProjections(), official_availability_source=Official())
    )
    assert "official_availability_failed:RuntimeError" in collected.warnings
    assert collected.game_availability == ()
    assert len(collected.inputs.projections) == 4


def test_collection_can_defer_projection_until_both_rosters_are_collected() -> None:
    provider = _RecordingProjections()
    collected = asyncio.run(_collect(_nba(), provider, skip_projections=True))
    assert len(collected.projection_targets) == 4
    assert collected.inputs.projections == ()
    assert provider.targets == []
