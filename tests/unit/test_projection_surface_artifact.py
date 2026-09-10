"""Artifact-boundary tests for immutable historical projection surfaces."""

from pathlib import Path

from lock_in_diagnostic_support import _projection_surface, _team_week

from sleeper_manager.backtesting.replay.projection_surface_artifact import (
    load_historical_projection_surface_artifact,
    write_historical_projection_surface_artifact,
)


def test_surface_round_trips_with_stable_bytes(tmp_path: Path) -> None:
    """Persist and reload a content-addressed projection surface deterministically."""

    team_week = _team_week()
    surface = _projection_surface(team_week)
    path = tmp_path / "surface.json"

    first_hash = write_historical_projection_surface_artifact(path, surface)
    first_bytes = path.read_bytes()
    second_hash = write_historical_projection_surface_artifact(path, surface)
    loaded = load_historical_projection_surface_artifact(
        path,
        team_week=team_week,
        projection_config_version="fixture-model",
    )

    assert first_hash == second_hash == surface.fingerprint
    assert path.read_bytes() == first_bytes
    assert loaded == surface
