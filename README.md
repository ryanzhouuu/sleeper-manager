# Sleeper Manager

Personal, read-only fantasy basketball assistant for Sleeper Lock-In leagues. It
combines Sleeper league data with NBA schedules, availability, and projections to
recommend weekly lineup and Lock-In decisions. Optional ntfy or Discord
notifications; it never submits roster changes to Sleeper.

See [LOCK_IN_MODE.md](LOCK_IN_MODE.md) for the league rules the decision engine
enforces.

## Local setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --all-groups
```

Create an ignored `.env`:

```dotenv
SLEEPER_LEAGUE_ID=your-league-id
SLEEPER_USER_ID=your-user-id
```

Optional manager policy (defaults work without one):

```bash
mkdir -p .local
cp manager-policy.example.toml .local/policy.toml
```

```bash
uv run sleeper-manager check-config
uv run sleeper-manager bootstrap
uv run sleeper-manager check-nba-data
```

Local commands persist to SQLite at `.local/state.db` by default.

## Configuration

Settings load from environment variables or `.env`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `SLEEPER_LEAGUE_ID` | League for live commands | Required |
| `SLEEPER_USER_ID` | Manager roster identity | Required |
| `TIMEZONE` | Display and scheduling timezone | `America/Chicago` |
| `MANAGER_POLICY_PATH` | Manager policy TOML | `.local/policy.toml` |
| `SQLITE_PATH` | Local state database | `.local/state.db` |
| `NTFY_TOPIC` | Enables ntfy delivery | Empty |
| `NTFY_BASE_URL` | ntfy service root | `https://ntfy.sh` |
| `NTFY_ACCESS_TOKEN` | Optional ntfy auth | Empty |
| `DISCORD_WEBHOOK_URL` | Enables Discord delivery | Empty |
| `ACKNOWLEDGEMENT_BASE_URL` | Notification action callbacks | Empty |

Notification commands need `NTFY_TOPIC` or `DISCORD_WEBHOOK_URL`. Interactive
notifications also need `ACKNOWLEDGEMENT_BASE_URL`.

## Commands

```bash
uv run sleeper-manager --help
```

| Command | Purpose |
| --- | --- |
| `check-config` | Report sanitized configuration readiness |
| `bootstrap` | Validate and summarize the configured league |
| `check-nba-data` | Report NBA provider health and mapping coverage |
| `test-notification` | Send one local notification diagnostic |
| `run-scheduled` | Run one local due-work wake against SQLite |

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=sleeper_manager --cov-report=term-missing
```
