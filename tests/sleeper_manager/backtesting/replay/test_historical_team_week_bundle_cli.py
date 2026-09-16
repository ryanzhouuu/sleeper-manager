"""Coverage for the historical team-week bundle command boundary."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

import sleeper_manager.backtesting.replay.team_week_bundle as team_week_bundle
from sleeper_manager.integrations.sleeper.client import SleeperAPIError
from tests.sleeper_manager.backtesting.replay.team_week_source_support import LEAGUE_ID


def test_module_main_reports_sleeper_acquisition_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return an operational error instead of a traceback when archive acquisition fails."""

    monkeypatch.setattr(team_week_bundle, "bootstrap_historical_team_week_bundle", _raise_sleeper)

    assert (
        team_week_bundle.main(
            [
                "--league-id",
                LEAGUE_ID,
                "--roster-id",
                "4",
                "--week",
                "16",
                "--monday",
                "2026-02-02",
            ]
        )
        == 2
    )
    assert "Sleeper request failed" in capsys.readouterr().out


def _raise_sleeper(*_args: object, **_kwargs: object) -> NoReturn:
    """Raise the provider failure used to verify the command's error path."""

    raise SleeperAPIError("Sleeper request failed")


def test_module_passes_explicit_inactive_evidence_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = tmp_path / "reviewed.json"

    def capture(*args: object, **kwargs: object) -> NoReturn:
        assert kwargs["inactive_evidence_path"] == ledger
        raise ValueError("Captured evidence path")

    monkeypatch.setattr(team_week_bundle, "bootstrap_historical_team_week_bundle", capture)
    assert (
        team_week_bundle.main(
            [
                "--league-id",
                LEAGUE_ID,
                "--roster-id",
                "4",
                "--week",
                "16",
                "--monday",
                "2026-02-02",
                "--inactive-evidence",
                str(ledger),
            ]
        )
        == 2
    )


def test_module_passes_explicit_final_roster_evidence_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = tmp_path / "reviewed-rosters.json"

    def capture(*args: object, **kwargs: object) -> NoReturn:
        assert kwargs["final_roster_evidence_path"] == ledger
        raise ValueError("Captured evidence path")

    monkeypatch.setattr(team_week_bundle, "bootstrap_historical_team_week_bundle", capture)
    assert (
        team_week_bundle.main(
            [
                "--league-id",
                LEAGUE_ID,
                "--roster-id",
                "4",
                "--week",
                "16",
                "--monday",
                "2026-02-02",
                "--final-roster-evidence",
                str(ledger),
            ]
        )
        == 2
    )
