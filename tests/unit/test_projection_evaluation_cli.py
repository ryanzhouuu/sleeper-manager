from pathlib import Path

from sleeper_manager.cli import build_parser


def test_cli_exposes_projection_evaluation_command_with_development_default() -> None:
    args = build_parser().parse_args(["evaluate-projections"])

    assert args.command == "evaluate-projections"
    assert args.mode == "development"
    assert args.workspace == Path(".local/model-validation")
    assert args.league_fixture == Path("tests/fixtures/sleeper/current_league.json")


def test_cli_projection_evaluation_command_accepts_locked_retrospective_mode() -> None:
    args = build_parser().parse_args(["evaluate-projections", "--mode", "locked_retrospective"])

    assert args.mode == "locked_retrospective"
