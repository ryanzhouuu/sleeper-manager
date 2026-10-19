# Cloudflare runtime

The Worker uses one five-minute Cron Trigger. Forecast capture exceeded the Workers
Free plan's 10 ms CPU allowance before it could write a receipt, so this schedule
requires the Workers Paid CPU allowance. Verify the first remote receipt after deploying.
Worker logs and traces are enabled with query strings redacted. Shadow events contain
run identities and failure counts; private roster evidence remains in D1 artifacts.

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

NBA team schedules use ESPN's end-year label (`2027` for Sleeper season `2026`)
and explicitly request the regular season. This also applies to capture-context
and pre-tipoff reads during preseason, when ESPN otherwise returns a preseason slate.
Workers try the primary public ESPN host, then its alternate after a transport error,
403, 429, or server error. Each endpoint gets at most two requests total. Missing
resources, invalid JSON, and schema drift fail explicitly; custom base URLs stay isolated.

Official game availability uses a Workers-native fetch adapter and the shared PDF
parser, now included in runtime dependencies. The adapter checks recent publication
slots directly rather than renewing an old cached designation. Missing publications,
provider failures, parsing failures, and oversized reports remain explicit gaps.
PDF publication must match its requested slot; fresh retrieval cannot renew a report
published more than one hour before the decision cutoff.
Live planning passes matched official game reports through the shared projection
batch boundary. Confirmed absences set only that game's distribution to zero,
including when the direct baseline has insufficient history. Questionable, doubtful,
and probable designations retain provisional estimates and warning evidence.
Collection can defer projection until both rosters have been read; async batch
providers receive all targets together instead of one call per player-game.
The direct baseline shares validated history and its index within a batch while
retaining each game's cutoff filtering and input identity. Hybrid fitting reuses
identical conditional weights within fixed preparation; availability and participation
remain game-specific. Decoded hybrid outcomes use a batch-local LRU capped at 8,192
retained rows and 64 partitions, in addition to the active player's required history.

State migration `0005_hybrid_runtime.sql` adds immutable compressed artifacts,
prepared history versions, and shadow run references. History manifests are published
only after their index, exact player outcomes, and original census partitions verify.
Shadow evidence uses the state database; captured external forecasts remain in the
separate forecast archive. The local SQLite adapter uses the same artifact contracts.

Runtime policy stores `hybrid_shadow_enabled` and `hybrid_history_version` separately
from `active_projection_provider`. Existing policies default to shadow disabled and
`direct_baseline` advice. Shadow activation requires a pinned prepared history version.
`active_projection_provider` explicitly selects `direct_baseline` or `hybrid` advice.
Hybrid advice also requires a pinned history version; it can run with shadow evaluation
enabled or disabled. An empty baseline history is allowed only as a diagnostic control
when hybrid supplies advice. Selected hybrid failures remain missing projections and
never silently substitute baseline advice. Existing policies retain baseline defaults.

Prepare or synchronize indexed hybrid history with a sanitized dry-run first:

```bash
uv run python -m sleeper_manager.cloudflare.hybrid_sync_cli \
  --history .local/hybrid/history.json --league-fixture .local/league.json --season 2026
```

Add `--apply` to upload to the state D1 database after applying its migrations.
Add `--enable-shadow` to update the existing runtime policy after the uploaded
artifacts verify. This preserves manager preferences and direct-baseline history;
it preserves the explicit active-provider selection. `--local-db .local/state.db --apply` writes the
same contracts to local SQLite. Dry-run never creates a database or activates policy.
The history reference cutoff and its actual publication time are retained separately;
runtime reads reject a version published after the requested decision cutoff.
Shadow runs likewise retain their decision cutoff and actual persistence time.
When shadow is enabled, the runtime resolves the opponent and batches both rosters
at one cutoff. Durable records pair control snapshots with candidate attempts,
including source/fallback evidence, participation, donor weights, and exclusions.
Shared large diagnostics are referenced by content hash. Candidate computation has
a 60-second async timeout; failures remain paired blockers. Persistence and opponent
collection failures are logged explicitly and preserve valid manager control advice.
Due forecast capture runs before planning so that wake's stored revision can be read.
Pre-tipoff planning is due one five-minute Cron interval before the configured move
deadline (15 minutes before tipoff with the default 10-minute lead). This preserves
the movement margin instead of collecting statuses when the deadline has already
arrived. Live collection and the final advice cutoff use completed collection time;
shadow work cannot preserve an earlier advice time after consuming that margin.
Lineup and Lock-In messages explain confirmed absences, provisional injury estimates,
and unavailable official reports. Source hashes remain in projection/shadow diagnostics.
Visible availability changes revise notification identity; identical refreshed warnings
deduplicate. Recommendation traces retain `availability_notes` and a separate
`availability_material_hash`, preserving the underlying plan's material identity.

Local `run-scheduled` uses the same hybrid artifact store in `SQLITE_PATH` and reads
external revisions from the neighboring `forecasts.db`. Its HTTP adapter supports
the Python Workers `bytes()` response API, including official report PDFs.

Validation on October 13, 2026 retained 105,409 stat observations and all 8,190 census
opportunities. Verified report reconstruction tagged 5,825 opportunities; 2,365 remain
applicability gaps. The league's full scoring policy yielded zero eligible hybrid stat
profiles because technical/flagrant coverage is unverified. Synchronization preserves
that blocker; a successful upload does not establish projection readiness.
An isolated local workerd test exercised real D1 reads, official PDF parsing, and a
24-player batch in 5.923 seconds using the explicitly diagnostic policy with foul
penalties disabled. This is integration evidence, not production scoring validation,
an enforced memory-budget measurement, or live accuracy evidence.
A separate local full-scoring check retained 53,013 exclusions, returned zero for
one confirmed absence with its conditional forecast gap, and preserved blockers for
the other 23 targets. Its D1 receipt verified the runtime persistence path.

Forecast receipts, raw payloads, and semantic revisions live in the separate
`sleeper-manager-forecast-archive` D1 database. Apply every file in
`infra/cloudflare/forecast-migrations/` before deploying a Worker with the
`forecast_archive` binding. The scheduled collector uses the existing five-minute
Cron wake but requests the feed only for due daily or pre-tipoff slots. Each stored
receipt also keeps the manager and opponent rosters, eligibility, matchup, and
current fantasy-week games read for that slot, or an explicit gap when one of those
reads fails. Capture does not change lineup or Lock-In advice.

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

Configure advice and evaluation separately in that TOML file:

```toml
[projections]
active_provider = "hybrid" # or "direct_baseline"
shadow_enabled = true
hybrid_history_version = "hybrid-runtime-v1-<verified-content-hash>"
```

Hybrid advice or shadow requires the explicit published version. The initial history
sync verifies newly selected hybrid artifacts, model, season, full-scoring policy, and
nonempty eligible profiles before activating policy. An omitted `[projections]` table
preserves an existing selection; an explicit table replaces it, including disabling shadow.

After initialization, change only the policy without rebuilding history or redeploying:

```bash
uv run python -m sleeper_manager.cloudflare.policy_sync_cli \
  --league-fixture .local/league.json
```

This dry-run reads D1 and verifies the selected evidence. Add `--apply` to activate
the verified selection; `--policy` selects a different TOML file. Use a retained league
payload with current `scoring_settings`; a mismatch blocks hybrid activation.
Remote access uses the same Cloudflare account/API-token environment variables as
the history sync. `--local-db .local/state.db` targets an already initialized local
runtime. Neither mode deploys code or rebuilds history. Runtime metadata verification
does not establish predictive calibration; run forecast/replay checks before activation.

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
