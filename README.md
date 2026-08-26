# Sleeper Manager

Sleeper Manager is a personal, read-only fantasy basketball assistant for Sleeper Lock-In
leagues. It combines Sleeper league data with NBA schedules, availability, and projections to
recommend weekly lineup and Lock-In decisions. It can send ntfy or Discord notifications, but
it never submits roster changes to Sleeper.

## How it works

- Sleeper provides league settings, rosters, matchups, transactions, and fantasy scoring.
- ESPN and official NBA injury reports provide current schedules, results, and availability.
- Historical SportsDataverse data supports projection experiments and backtesting.
- Local commands persist state in SQLite; the deployed Cloudflare Worker uses D1.
- Notification actions record acknowledgements in Sleeper Manager only. The manager still
  makes every roster or Lock-In change in Sleeper.

See [LOCK_IN_MODE.md](LOCK_IN_MODE.md) for the league rules enforced by the decision engine.

## Local setup

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)

Install the locked development environment:

```bash
uv sync --locked --all-groups
```

Create an ignored `.env` with the Sleeper IDs used for live commands:

```dotenv
SLEEPER_LEAGUE_ID=your-league-id
SLEEPER_USER_ID=your-user-id
```

Do not commit `.env`, notification topics, access tokens, or webhook URLs. The default manager
policy works without a file; to customize it, copy the documented example into the ignored
local directory:

```bash
mkdir -p .local
cp manager-policy.example.toml .local/policy.toml
```

Verify configuration, synchronize the league profile, and inspect NBA data health:

```bash
uv run sleeper-manager check-config
uv run sleeper-manager bootstrap
uv run sleeper-manager check-nba-data
```

`bootstrap` and `check-nba-data` read from external providers and write only to the local SQLite
database at `.local/state.db` by default.

## Configuration

Settings load from environment variables or the ignored `.env` file.

| Variable | Purpose | Default |
| --- | --- | --- |
| `SLEEPER_LEAGUE_ID` | League used by live commands | Required for live commands |
| `SLEEPER_USER_ID` | Manager account used to identify the roster | Required for live commands |
| `TIMEZONE` | Local display and scheduling timezone | `America/Chicago` |
| `MANAGER_POLICY_PATH` | Manager policy TOML file | `.local/policy.toml` |
| `SQLITE_PATH` | Local state and NBA cache database | `.local/state.db` |
| `NTFY_TOPIC` | Enables ntfy delivery | Empty |
| `NTFY_BASE_URL` | ntfy service root | `https://ntfy.sh` |
| `NTFY_ACCESS_TOKEN` | Optional ntfy authentication | Empty |
| `DISCORD_WEBHOOK_URL` | Enables Discord delivery | Empty |
| `ACKNOWLEDGEMENT_BASE_URL` | Base URL for notification action callbacks | Empty |

At least one of `NTFY_TOPIC` or `DISCORD_WEBHOOK_URL` is required for notification commands.
`ACKNOWLEDGEMENT_BASE_URL` is also required for interactive notifications and scheduled work.

## Commands

Run `uv run sleeper-manager --help` for the complete argument reference.

| Command | Purpose |
| --- | --- |
| `check-config` | Report sanitized configuration readiness |
| `bootstrap` | Validate and summarize the configured Sleeper league |
| `check-nba-data` | Report NBA provider health and player-mapping coverage |
| `phase3-test-notification` | Send one idempotent local notification diagnostic |
| `run-scheduled` | Run one local due-work wake against SQLite |
| `validate-model-features` | Run the frozen historical feature experiment |
| `evaluate-projections` | Evaluate the frozen projection models |
| `validate-lock-in-policy` | Replay historical leagues against the Lock-In policy |
| `sync-cloudflare-runtime-data` | Prepare or apply projection history and runtime policy in D1 |

Historical ingestion commands need the optional dependencies:

```bash
uv sync --locked --all-groups --extra historical
```

## Development

Run the same checks enforced by CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=sleeper_manager --cov-report=term-missing
```

The opt-in live provider smoke test is excluded from the default suite. Run it only with
`FANTASY_MANAGER_LIVE_SMOKE=1` and configured Sleeper IDs.

## Cloudflare runtime

The production path is a Python Worker with D1 persistence and a five-minute Cron Trigger.
Projection history and the active runtime policy are synchronized separately from deployment.

See [docs/cloudflare-runtime.md](docs/cloudflare-runtime.md) for operator commands and dry-run
behavior, and [infra/cloudflare/README.md](infra/cloudflare/README.md) for first-deployment and
migration steps.

## Repository layout

- `src/sleeper_manager/domain`: fantasy, scoring, scheduling, and planning models
- `src/sleeper_manager/integrations`: Sleeper and NBA provider adapters
- `src/sleeper_manager/decisions`: lineup and Lock-In decision policies
- `src/sleeper_manager/workflows`: planning, notification, and diagnostic workflows
- `src/sleeper_manager/persistence`: SQLite and D1 repositories
- `src/sleeper_manager/projections`: live and historical projection models
- `src/sleeper_manager/backtesting`: replay, experiment, and validation tooling
- `src/sleeper_manager/cloudflare`: Worker entry point and runtime adapters
- `infra/cloudflare`: D1 migrations and deployment notes
- `tests`: deterministic unit tests and provider fixtures
