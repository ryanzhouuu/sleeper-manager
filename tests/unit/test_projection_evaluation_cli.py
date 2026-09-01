from pathlib import Path

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


def test_cli_dispatches_projection_evaluation_to_command_handler(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def run_command(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 7

    monkeypatch.setattr("sleeper_manager.cli.run_projection_evaluation_command", run_command)

    assert main(["evaluate-projections", "--mode", "locked_retrospective"]) == 7
    assert captured["mode"] == "locked_retrospective"
