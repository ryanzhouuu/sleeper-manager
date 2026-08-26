"""Pin CLI command names and fail-closed dispatch for local operator commands."""

from __future__ import annotations

import pytest

from sleeper_manager.cli import build_parser, main
from sleeper_manager.config import Settings

OPERATIONAL_COMMANDS = (
    "check-config",
    "bootstrap",
    "check-nba-data",
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
