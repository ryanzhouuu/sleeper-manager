# Cloudflare runtime

The Worker uses one five-minute Cron Trigger. Each wake claims due daily, pre-tipoff, or
delivery-retry work and runs the weekly lineup planner at most once. Lineup notifications
include Open Sleeper only. The `test-notification` command remains a local diagnostic
and is never invoked from `scheduled()`.

Projection history and the active runtime policy live in D1. They are not bundled into the
Worker deploy.

## Operator commands

From the repository root, synchronize compact direct-baseline history and then deploy:

```bash
uv run --extra historical sleeper-manager sync-cloudflare-runtime-data --apply
uvx --from workers-py pywrangler deploy
```

`sync-cloudflare-runtime-data` is a dry-run by default. It reconstructs compact observations
from ignored local validation artifacts under `.local/model-validation`, reports dataset
version, observation count, content hash, and the runtime policy that would be activated, and
writes nothing. `--apply` writes the observations to D1, verifies count and hash, and
activates runtime policy last.

`--apply` requires `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the environment. Do
not print those values; Wrangler secrets stay in the Worker environment:

- `NTFY_TOPIC`
- `NTFY_ACCESS_TOKEN` (optional)
- `DISCORD_WEBHOOK_URL` (optional)
- `SLEEPER_LEAGUE_ID`
- `SLEEPER_USER_ID`

Apply D1 migrations before the first sync if the database is new:

```bash
npx wrangler d1 migrations apply sleeper-manager-state --remote
```

## Local due-work wake

To run the same dispatcher against SQLite:

```bash
uv run sleeper-manager run-scheduled
```

This path is read-only toward Sleeper. It fails closed when runtime policy or projection
history is missing.
