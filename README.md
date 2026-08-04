# Sleeper Manager

Sleeper Manager is a read-only fantasy basketball advisor for Sleeper Lock-In leagues. It collects league and NBA data, evaluates lineup and Lock-In decisions, and sends actionable phone notifications. It never submits roster changes to Sleeper.

## Status

The repository currently contains the validated project foundation. Decision models and production deployment will be implemented incrementally and tested in shadow mode before the 2026–27 regular season.

## Data sources

- Sleeper's documented public API is authoritative for league settings, rosters, matchups, transactions, and fantasy point totals.
- ESPN's public JSON feeds provide live schedules, box scores, play-by-play, rosters, and injuries through a replaceable adapter.
- SportsDataverse provides cached historical ESPN player box scores for projections and backtesting.
- Sleeper injury status is used as a secondary availability signal.

Sleeper does not expose an explicit Lock-In flag. Lock actions will therefore be recorded when the manager acknowledges a recommendation using an ntfy action button.

## Local setup

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)

```bash
cp .env.example .env
uv sync --all-groups
uv run sleeper-manager check-config
uv run sleeper-manager bootstrap
uv run pytest
```

`bootstrap` performs a read-only Sleeper synchronization, validates the discovered NBA
Lock-In configuration, stores the local league fingerprint, and prints a sanitized summary.
Manager decision settings are loaded from `.local/policy.toml`; copy
`manager-policy.example.toml` there to customize the documented defaults.

## Repository layout

- `src/sleeper_manager/domain`: platform-independent fantasy models and scoring
- `src/sleeper_manager/integrations`: Sleeper and NBA provider adapters
- `src/sleeper_manager/decisions`: lineup and Lock-In decision policies
- `src/sleeper_manager/workflows`: scheduled application workflows
- `src/sleeper_manager/persistence`: local and cloud state backends
- `src/sleeper_manager/notifications`: ntfy and Discord delivery
- `src/sleeper_manager/handlers`: AWS Lambda entry points
- `infra/aws`: AWS SAM infrastructure
- `tests`: unit and integration tests

See [LOCK_IN_MODE.md](LOCK_IN_MODE.md) for the league-mode rules used by the decision engine.
