# Cloudflare runtime

The Worker uses one five-minute Cron Trigger. Forecast capture exceeded the Workers
Free plan's 10 ms CPU allowance before it could write a receipt, so this schedule
requires the Workers Paid CPU allowance. Verify the first remote receipt after deploying.

Each wake claims due daily, pre-tipoff, postgame, or delivery-retry work. Daily and
pre-tipoff wakes run the weekly lineup planner at most once and refresh live Lock-In
opportunity rows.
Postgame wakes fetch ESPN game summaries directly, stabilize finals, and may send
Lock, Pass, or unavailable-warning notifications. Lineup notifications include
Open Sleeper only.
Lock-In notifications include Locked, Passed, and Open Sleeper. The `test-notification`
command remains a local diagnostic and is never invoked from `scheduled()`.

Projection history and the active runtime policy live in D1. They are not bundled into the
Worker deploy. Without an active runtime policy, advisor planning stays blocked while
forecast capture uses the default 7:00 AM America/Chicago daily schedule.

Forecast receipts, raw payloads, and semantic revisions live in the separate
`sleeper-manager-forecast-archive` D1 database. Apply
`infra/cloudflare/forecast-migrations/0001_forecast_archive.sql` before deploying a Worker
with the `forecast_archive` binding. The scheduled collector uses the existing five-minute
Cron wake but requests the feed only for due daily or pre-tipoff slots. Capture does not
change lineup or Lock-In advice.

If the NBA tipoff lookup fails, the Worker logs the failure and retries on later wakes
without caching an empty slate. The due daily forecast request still runs. Pre-tipoff
slots cannot be scheduled until the lookup succeeds; treat that interval as a capture
coverage gap.

## Operator commands

From the repository root, deploy capture only after applying both D1 migration sets:

```bash
npx wrangler d1 migrations apply sleeper-manager-state --remote
npx wrangler d1 migrations apply sleeper-manager-forecast-archive --remote
uvx --from workers-py pywrangler deploy
```

To activate advisor planning and notifications later, synchronize compact
direct-baseline history and deploy:

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

Apply D1 migrations before the first sync if either database is new.

## Remote forecast archive checks

Use Wrangler from the repository root so it selects the forecast binding and migration
directory. These queries read only aggregate counts and receipt metadata; they do not
print raw forecast payloads.

```bash
npx wrangler d1 execute sleeper-manager-forecast-archive --remote --command \
  "SELECT receipt_id, season, season_type, scheduled_for, persisted_at, outcome, timing, error_code FROM forecast_fetch_receipts ORDER BY persisted_at DESC, receipt_id DESC LIMIT 5"
npx wrangler d1 execute sleeper-manager-forecast-archive --remote --command \
  "SELECT COUNT(*) AS receipts, COALESCE(SUM(outcome IN ('changed', 'unchanged')), 0) AS usable, COALESCE(SUM(outcome IN ('failed', 'invalid', 'suppressed') OR timing = 'late'), 0) AS gaps FROM forecast_fetch_receipts"
npx wrangler d1 execute sleeper-manager-forecast-archive --remote --command \
  "SELECT COALESCE((SELECT SUM(LENGTH(encoded_payload)) FROM forecast_raw_artifacts), 0) + COALESCE((SELECT SUM(LENGTH(encoded_records)) FROM forecast_revisions), 0) AS encoded_payload_bytes"
```

After the first due daily slot, confirm a new receipt exists. A missing receipt is itself
an operational gap even when the gap query returns zero; inspect the Cron summary and
Worker logs. A clean capture has `outcome` of `changed` or `unchanged` and `timing` of
`on_time`. Compare `encoded_payload_bytes` with the 350,000,000-byte alert threshold.
The database file size can differ from that payload sum. When a forecast changes,
verify a new revision row appears; an unchanged response should add a receipt without
adding a revision.

## Local due-work wake

To run the same dispatcher against SQLite:

```bash
uv run sleeper-manager run-scheduled
```

This path is read-only toward Sleeper. It fails closed when runtime policy or projection
history is missing.
