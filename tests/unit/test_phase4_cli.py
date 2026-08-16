from pathlib import Path

from sleeper_manager.cli import build_parser


def test_cli_exposes_phase4_validation_command_with_development_default() -> None:
    args = build_parser().parse_args(["validate-phase4"])

    assert args.command == "validate-phase4"
    assert args.mode == "development"
    assert args.workspace == Path(".local/model-validation")
    assert args.league_fixture == Path("tests/fixtures/sleeper/current_league.json")


def test_cli_phase4_validation_command_accepts_locked_retrospective_mode() -> None:
    args = build_parser().parse_args(["validate-phase4", "--mode", "locked_retrospective"])

    assert args.mode == "locked_retrospective"
