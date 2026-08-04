import argparse

from sleeper_manager import __version__
from sleeper_manager.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sleeper fantasy basketball decision assistant")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check-config", help="Report whether required configuration is present")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-config":
        settings = Settings()
        print(f"Sleeper configured: {settings.sleeper_configured}")
        print(f"Notifications configured: {settings.notifications_configured}")
        print(f"State backend: {settings.state_backend}")
        print(f"Timezone: {settings.timezone}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
