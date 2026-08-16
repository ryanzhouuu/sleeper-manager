from sleeper_manager.cli import build_parser


def test_cli_exposes_additive_lock_in_validation_command() -> None:
    args = build_parser().parse_args(
        [
            "validate-lock-in-policy",
            "--current-league-id",
            "current",
            "--historical-league-id",
            "historical",
            "--stress-league-id",
            "stress",
        ]
    )
    assert args.command == "validate-lock-in-policy"
    assert args.refresh_sleeper is False
