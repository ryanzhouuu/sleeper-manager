from pathlib import Path

import pytest

from sleeper_manager.cli import build_parser, main


def test_cli_exposes_projection_evaluation_command_with_development_default() -> None:
    args = build_parser().parse_args(["evaluate-projections"])

    assert args.command == "evaluate-projections"
    assert args.mode == "development"
    assert args.workspace == Path(".local/model-validation")
    assert args.league_fixture == Path("tests/fixtures/sleeper/current_league.json")


def test_cli_projection_evaluation_command_accepts_locked_retrospective_mode() -> None:
    args = build_parser().parse_args(["evaluate-projections", "--mode", "locked_retrospective"])

    assert args.mode == "locked_retrospective"


def test_cli_help_explains_locked_checkpoint_requirement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Locked help promises checkpoint reuse and explicitly rules out development fallback."""
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["evaluate-projections", "--help"])

    assert exit_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "matching completed development checkpoint" in output
    assert "never reruns development folds" in output


def test_cli_dispatches_projection_evaluation_to_command_handler(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def run_command(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 7

    monkeypatch.setattr("sleeper_manager.cli.run_projection_evaluation_command", run_command)

    assert main(["evaluate-projections", "--mode", "locked_retrospective"]) == 7
    assert captured["mode"] == "locked_retrospective"
