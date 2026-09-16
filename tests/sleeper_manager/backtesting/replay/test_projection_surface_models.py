"""Contract tests for immutable historical projection-surface records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay.projection_surface_models import (
    HistoricalProjectionSurface,
    HistoricalProjectionSurfaceError,
    ProjectionSurfaceEntry,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot


def _projection(*, available_as_of: datetime) -> ProjectionSnapshot:
    """Build one deterministic projection for contract validation."""

    return ProjectionSnapshot(
        player_id="p1",
        game_id="g1",
        available_as_of=available_as_of,
        model_version="model-v1",
        input_version="inputs-v1",
        scoring_policy_version="scoring-v1",
        distribution=ProjectionDistribution.from_weighted_observations(((10.0, 1.0),)),
        reasons=(),
    )


def test_surface_rejects_projection_at_a_different_decision_time() -> None:
    """Reject a snapshot whose availability differs from its recorded cutoff."""

    decision_time = datetime(2026, 2, 2, 18, tzinfo=UTC)

    with pytest.raises(HistoricalProjectionSurfaceError, match="decision time"):
        ProjectionSurfaceEntry(
            decision_time,
            "p1",
            "g1",
            projection=_projection(available_as_of=decision_time + timedelta(seconds=1)),
        )


def test_surface_fingerprint_is_stable_across_input_order() -> None:
    """Canonicalize logically unordered entries before fingerprinting."""

    first_cutoff = datetime(2026, 2, 2, 18, tzinfo=UTC)
    second_cutoff = first_cutoff + timedelta(hours=1)
    entries = tuple(
        ProjectionSurfaceEntry(cutoff, "p1", "g1", projection=_projection(available_as_of=cutoff))
        for cutoff in (second_cutoff, first_cutoff)
    )
    surface = HistoricalProjectionSurface(
        team_week_manifest_id="manifest-1",
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        team_week_fingerprint="team-week-v1",
        projection_config_version="model-v1",
        scoring_policy_version="scoring-v1",
        source_fingerprints=(),
        entries=entries,
    )

    assert surface.entries == tuple(reversed(entries))
    assert surface.fingerprint == surface.fingerprint
    assert surface.to_dict()["fingerprint"] == surface.fingerprint
