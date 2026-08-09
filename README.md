# Sleeper Manager

Sleeper Manager is a read-only fantasy basketball advisor for Sleeper Lock-In leagues. It collects league and NBA data, evaluates lineup and Lock-In decisions, and sends actionable phone notifications. It never submits roster changes to Sleeper.

## Status

The repository currently contains the validated project foundation and the Phase 3 notification/acknowledgement operational slice. The deployed Phase 3 target is a Cloudflare Python Worker with D1 persistence. Decision models and production deployment will be implemented incrementally and tested in shadow mode before the 2026–27 regular season.

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
uv sync --all-groups --extra historical
uv run sleeper-manager check-config
uv run sleeper-manager bootstrap
uv run pytest
```

`bootstrap` performs a read-only Sleeper synchronization, validates the discovered NBA
Lock-In configuration, stores the local league fingerprint, and prints a sanitized summary.
Manager decision settings are loaded from `.local/policy.toml`; copy
`manager-policy.example.toml` there to customize the documented defaults.

After configuring `ACKNOWLEDGEMENT_BASE_URL` and a notification destination, the
Phase 3 operational slice can be exercised with:

```bash
uv run sleeper-manager phase3-test-notification
```

The `Locked` and `Passed` actions record the manager's acknowledgement only; they
never submit a change to Sleeper.

## Cloudflare deployment

The deployed Phase 3 runtime uses a Python Worker, a D1 database, and a five-minute
Cron Trigger. Python Worker tooling requires Node.js and the `workers-py` package.

1. Create a D1 database and copy its ID into `infra/cloudflare/wrangler.toml`:

   ```bash
   npx wrangler d1 create sleeper-manager-state
   ```

2. Set `ACKNOWLEDGEMENT_BASE_URL` to the Worker URL plus `/ack`, and configure the
   Sleeper IDs and notification values as Worker variables or secrets.
3. Apply the schema and deploy:

   ```bash
   npx wrangler d1 migrations apply sleeper-manager-state --remote
   uvx --from workers-py pywrangler deploy
   ```

   The Worker URL is printed after deployment. A first deployment may be needed to
   discover the URL before setting `ACKNOWLEDGEMENT_BASE_URL` and redeploying.

Use `uvx --from workers-py pywrangler dev` for local Worker development. The local
Python CLI continues to use SQLite and is useful for deterministic tests.

## Repository layout

- `src/sleeper_manager/domain`: platform-independent fantasy models and scoring
- `src/sleeper_manager/integrations`: Sleeper and NBA provider adapters
- `src/sleeper_manager/decisions`: lineup and Lock-In decision policies
- `src/sleeper_manager/workflows`: scheduled application workflows
- `src/sleeper_manager/persistence`: local SQLite and D1 state backends
- `src/sleeper_manager/notifications`: ntfy and Discord delivery
- `src/sleeper_manager/cloudflare`: Python Worker entry point and Cloudflare adapters
- `infra/cloudflare`: Wrangler configuration and D1 migrations
- `tests`: unit and integration tests

See [LOCK_IN_MODE.md](LOCK_IN_MODE.md) for the league-mode rules used by the decision engine.
