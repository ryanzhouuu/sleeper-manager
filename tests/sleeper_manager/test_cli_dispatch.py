"""Pin CLI command names and fail-closed dispatch for local operator commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from sleeper_manager.cli import build_parser, main
from sleeper_manager.config import Settings

OPERATIONAL_COMMANDS = (
    "check-config",
    "bootstrap",
    "check-nba-data",
    "check-forecast-capture",
    "test-notification",
    "run-scheduled",
    "sync-cloudflare-runtime-data",
    "validate-model-features",
    "evaluate-projections",
    "validate-lock-in-policy",
)


@pytest.fixture
def isolated_cli_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SLEEPER_LEAGUE_ID",
        "SLEEPER_USER_ID",
        "NTFY_TOPIC",
        "NTFY_ACCESS_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "ACKNOWLEDGEMENT_BASE_URL",
        "STATE_BACKEND",
        "TIMEZONE",
        "SQLITE_PATH",
        "MANAGER_POLICY_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("sleeper_manager.cli.Settings", lambda: Settings(_env_file=None))


def test_parser_exposes_current_command_names() -> None:
    help_text = build_parser().format_help()
    for name in OPERATIONAL_COMMANDS:
        assert name in help_text


def test_notification_command_is_registered() -> None:
    args = build_parser().parse_args(["test-notification"])
    assert args.command == "test-notification"


def test_legacy_phase3_command_name_is_not_registered() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["phase3-test-notification"])


def test_check_config_reports_unconfigured_defaults(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-config"]) == 0
    output = capsys.readouterr().out
    assert "Sleeper configured: False" in output
    assert "Notifications configured: False" in output
    assert "State backend: sqlite" in output
    assert "Timezone: America/Chicago" in output


def test_check_config_reports_configured_sleeper(
    isolated_cli_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "league-1")
    monkeypatch.setenv("SLEEPER_USER_ID", "user-1")
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    assert main(["check-config"]) == 0
    output = capsys.readouterr().out
    assert "Sleeper configured: True" in output
    assert "Notifications configured: True" in output
    assert "Timezone: America/New_York" in output


def test_bootstrap_requires_sleeper_ids(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["bootstrap"]) == 2
    assert "SLEEPER_LEAGUE_ID" in capsys.readouterr().err


def test_check_nba_data_rejects_invalid_date(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-nba-data", "--date", "08-26-2026"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_check_nba_data_requires_sleeper_ids(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-nba-data"]) == 2
    assert "SLEEPER_LEAGUE_ID" in capsys.readouterr().err


def test_notification_requires_notification_config(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["test-notification"]) == 2
    assert "Notification configuration is incomplete" in capsys.readouterr().err


def test_notification_requires_acknowledgement_url(
    isolated_cli_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    assert main(["test-notification"]) == 2
    assert "ACKNOWLEDGEMENT_BASE_URL" in capsys.readouterr().err


def test_check_forecast_capture_rejects_non_sqlite_backend(
    isolated_cli_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _RemoteBackend:
        state_backend = "d1"
        sqlite_path = Path("state.db")

    monkeypatch.setattr("sleeper_manager.cli.Settings", lambda: _RemoteBackend())
    assert main(["check-forecast-capture"]) == 2
    assert "STATE_BACKEND=sqlite" in capsys.readouterr().err


def test_check_forecast_capture_reports_a_missing_archive(
    isolated_cli_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    assert main(["check-forecast-capture"]) == 0
    output = capsys.readouterr().out
    assert "Last capture: none" in output
    assert not (tmp_path / "forecasts.db").exists()


def test_check_forecast_capture_alerts_when_the_archive_cannot_be_read(
    isolated_cli_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "forecasts.db").mkdir()
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    assert main(["check-forecast-capture"]) == 2
    assert "Forecast capture health failed" in capsys.readouterr().err


def test_check_forecast_capture_alerts_on_the_latest_failure(
    isolated_cli_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
    from tests.sleeper_manager.persistence.forecast_sqlite_support import failed_capture

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    archive.save_capture(failed_capture())
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    assert main(["check-forecast-capture"]) == 1
    assert "Alert: last capture is not a clean success" in capsys.readouterr().out


def test_run_scheduled_requires_sleeper_ids(
    isolated_cli_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run-scheduled"]) == 2
    assert "SLEEPER_LEAGUE_ID" in capsys.readouterr().err


def test_run_scheduled_requires_notification_config(
    isolated_cli_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "league-1")
    monkeypatch.setenv("SLEEPER_USER_ID", "user-1")
    assert main(["run-scheduled"]) == 2
    assert "Notification configuration is incomplete" in capsys.readouterr().err


def test_run_scheduled_requires_acknowledgement_url(
    isolated_cli_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "league-1")
    monkeypatch.setenv("SLEEPER_USER_ID", "user-1")
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    assert main(["run-scheduled"]) == 2
    assert "ACKNOWLEDGEMENT_BASE_URL" in capsys.readouterr().err
