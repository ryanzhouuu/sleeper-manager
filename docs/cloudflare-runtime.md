# Cloudflare runtime

The Worker uses one five-minute Cron Trigger. Each wake claims due daily, pre-tipoff,
postgame, or delivery-retry work. Daily and pre-tipoff wakes run the weekly lineup
planner at most once and refresh live Lock-In opportunity rows. Postgame wakes fetch
ESPN game summaries directly, stabilize finals, and may send Lock, Pass, or
unavailable-warning notifications. Lineup notifications include Open Sleeper only.
Lock-In notifications include Locked, Passed, and Open Sleeper. The `test-notification`
command remains a local diagnostic and is never invoked from `scheduled()`.

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

The sync also translates the local manager policy TOML (`.local/policy.toml` by default) into
the runtime policy envelope. That translation copies decision preset and confidence, quiet-hour
settings, mapping overrides, and a content hash used as `manager_policy_version` on live plans.
`protected_sleeper_ids` is rejected in new TOML and accepted only as an empty legacy runtime
value. Quiet-hour fields are stored for later use; quiet-hour suppression is not enforced.
Live Lock-In evaluation consumes the resolved minimum-confidence threshold. Operational fields
such as freshness windows and the pinned projection-history version remain runtime defaults
unless changed in code.

Postgame watches start two hours after tipoff and retry every five minutes until the
opportunity is acknowledged, expired, or otherwise closed. Lock and Pass notifications
include Locked, Passed, and Open Sleeper actions. Unavailable warnings include Open Sleeper
only. Acknowledgement updates recommendation and opportunity state together; a later ESPN
stat correction refreshes the locked score without sending another action.

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
