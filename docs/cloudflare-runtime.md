# Cloudflare runtime

The Worker uses one five-minute Cron Trigger and the `sleeper-manager-planning` queue.
Cron runs the delay watchdog and forecast capture, then publishes a small text wake
signal through `PLANNING_QUEUE`. The consumer claims due work from D1 using its actual
execution time, resolves current policy and evidence, and runs planning and delivery.
The consumer permits one message and one invocation at a time, with a configured
300,000 ms CPU budget. It has a twelve-minute async execution timeout, below the
fifteen-minute D1 work lease. Duplicate signals cannot bypass work or delivery claims;
invalid signals and signals older than ten minutes are discarded. Cron republishes
every five minutes, so lost or exhausted transport retries do not erase durable work.
An enqueue failure leaves work unclaimed. Without a queue binding, local commands
retain inline execution.
Unexpected failures after claims release still-owned unfinished work for retry five
minutes after failure. Cancellation attempts the same cleanup and then propagates;
failed cleanup propagates for queue redelivery. Hard termination still relies on lease
expiry. Completion and retry times use the live clock, and ensuring today's daily row
preserves an existing retry time. Stage/error categories are logged without payloads.

Forecast capture exceeded the Workers
Free plan's 10 ms CPU allowance before it could write a receipt, so this schedule
requires the Workers Paid CPU allowance. Verify the first remote receipt after deploying.
Worker logs and traces are enabled with query strings redacted.
Scheduled stage events log entry and completion for forecast capture, planning collection,
and planning with delivery. Durations are wall time; use Cloudflare invocation metrics
for CPU time. Workers clocks may remain fixed during synchronous computation, so a
zero-duration projection stage does not establish negligible CPU usage. A stage entry
without completion can identify interrupted work even when
termination prevents a final summary.
Collection also reports league synchronization, baseline history loading, manager
evidence, and hybrid projection.
Candidate stages separate index loading, population preparation, forecast reads, and
each player's partition loading, pool preparation, and projection. Repeated stage pairs
show batch progress without logging player identities or private provider payloads.
Before forecast capture or collection, the watchdog checks today's unfinished daily
work. If it has not completed fifteen minutes after the manager-local daily schedule,
missing, pending, retrying, and running work trigger a
`Daily plan delayed` notification through the configured destinations. Delivery is
deduplicated per league and manager-local day; unsuccessful delivery can retry.
An active retry lease cannot suppress a delay alert. Completed or canceled work
stays quiet; prior-day and recovery jobs do not satisfy today's daily completion.
These operational records use decision type `daily_plan_delayed`, fantasy week `0`,
and player `system` to keep them separate from weekly advice. They have no notification actions.
Watchdog failures are logged without blocking planning.
Notifications go only to the `DISCORD_WEBHOOK_URL` webhook. A failed delivery records the
Discord HTTP status without the response text or payload.

Each planning consumer claims due daily, pre-tipoff, postgame, or delivery-retry work. Daily and
pre-tipoff wakes run the weekly lineup planner at most once and refresh live Lock-In
opportunity rows.
Live daily, pre-tipoff, manual, and postgame plans use 128 outer scenarios and 32
postgame continuation scenarios, reduced from 2,000 and 128 to favor interactive
latency. Sampling remains deterministic for a given input and seed, but this smaller
budget can change estimated values, confidence, and recommendations. Explicit
research/test policies retain their configured sampling budgets. Joint completed-score
comparison has no placement-count or continuation-count cutoff. It shares random
draws across alternatives, reuses opposing-action comparisons, and computes the
diagnostic upper bound only for the selected continuation. Move verification and
runtime resource limits still apply; interrupted work does not publish partial advice.
The first daily or pre-tipoff run of each manager-local day also projects the current
opponent's roster with the active provider and caches those game outcomes until local
midnight; postgame wakes never do. The dashboard combines that cache, Sleeper's live
opponent locks, and the plan's simulated team finishes into a win probability. It assumes
each opponent starter locks a finished game only when it beats waiting, reads Sleeper stats
only for an unlocked opponent game 3.5 hours after tipoff, and caches them for the day. Any
missing piece leaves the probability null.
With `objective = "win_chance"` under `[decision]`, daily, pre-tipoff, and postgame runs
also read the opponent's live locks and that cache. Starters and Lock/Pass choices then
maximize the chance of beating the opponent; choices within half a percentage point fall
back to expected points. Simulated later Lock/Pass choices inside a continuation still
maximize points. Completed-score search prunes on the win chance of each placement's
hindsight bound, which never understates its real win chance. Without opponent evidence
the run uses expected points, and each trace records `decision_objective`.
Pre-tipoff watches cover only the current fantasy week. Unchanged watches do not
write or reset retry timing. Each reconciliation cancels at most 50 obsolete future
watches, so legacy rows retire gradually without a large cleanup burst.
D1 schedule upserts retry recognized transient connection/reset failures at most
twice with short jittered backoff. Work claims and other writes do not gain those retries.
Postgame wakes fetch ESPN game summaries directly to detect finals, score rostered
players from Sleeper's per-game stats, wait while a played game's row is missing or
empty, stabilize finals, and may send Lock, Pass, or unavailable-warning notifications.
A final whose Lock and Pass confidences both fall below the minimum is sent as "Up to you":
the message gives the lock slot, each side's projected week, the share of simulations in
which each side finishes ahead, and each side's win chance under a win objective. Its
`up_to_you` advice record settles the row like Lock or Pass advice. Plan identity counts
close calls only by player and game, so shifting confidences do not re-send the message.
Once every due row for a watched game has a stable score whose latest poll matches it and
pending advice (Lock, Pass, up-to-you, or an unavailable warning), whether the row is Actionable
or back in Finalizing after a repoll, each five-minute wake reads only Sleeper's matchup legs. It skips collection and replanning
while Sleeper's submitted locks match the stored Sleeper-synced rows, and runs a full pass
when locks differ or Sleeper is unavailable. Before collecting, each wake also reads the ESPN
summary of every unsettled claimed game; when each one is still in progress, incomplete, or
missing a rostered player, it records that wait and skips collection. A game ESPN still reports
in progress is rechecked after 15 minutes; other waits and settled games keep the five-minute
wake. Wakes that do collect read those summaries again after collection. Scheduled daily, pre-tipoff, and other games'
postgame runs still replan settled games with fresh injury and lineup evidence.
Lineup notifications include
advice without action links. Scheduled daily no-change results send one
`daily_plan_completed` confirmation per manager-local day, including when daily and
pre-tipoff work coalesce. The confirmation preserves availability warnings, uses
normal delivery claims and retries, and expires at local midnight or fantasy-week
end, whichever comes first. Other unchanged pre-tipoff results stay quiet.
Lock-In notifications include Locked and Passed acknowledgement links. Notifications
do not append an Open Sleeper link. The `test-notification`
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

Each planning run fetches a fresh ESPN injury snapshot, shared by manager and opponent
collection, and updates the persisted cache with the provider's original timestamps.
Team rosters and schedules retain their existing cache windows. Injury fetch failures
produce the `injury_data_unavailable` warning instead of blocking advice; planning does
not substitute the previous cached snapshot or treat missing designations as healthy.
Valid official game reports still apply. Actionable lineup and Lock/Pass notifications
include a provisional-availability warning. Daily no-change confirmations preserve
the same warnings; other no-action lineup plans stay quiet.
Successful but expired injury snapshots still fail the fifteen-minute freshness check
at the final decision cutoff. Roster, schedule, identity, projection, and deadline
failures retain their existing blocking rules.

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

State migration `0005_hybrid_runtime.sql` adds immutable compressed artifacts and
prepared history versions. History manifests are published only after their index, exact
player outcomes, and original census partitions verify. Captured external forecasts
remain in the separate forecast archive. The local SQLite adapter uses the same artifact
contracts. Migration `0008_drop_hybrid_shadow_runs.sql` removes the retired live shadow
table; apply it after deploying code that no longer writes shadow runs.

`active_projection_provider` explicitly selects `direct_baseline` or `hybrid` advice.
Hybrid advice requires a pinned `hybrid_history_version`. Selected hybrid failures remain
missing projections and never silently substitute baseline advice. Existing policies
retain baseline defaults. Live shadow evaluation was retired: stored policies may still
carry `hybrid_shadow_enabled: false`, and a policy that sets it to `true` is rejected.

Prepare or synchronize indexed hybrid history with a sanitized dry-run first:

```bash
uv run python -m sleeper_manager.cloudflare.hybrid_sync_cli \
  --history .local/hybrid/history.json --league-fixture .local/league.json --season 2026
```

Add `--apply` to upload to the state D1 database after applying its migrations.
Publishing history never changes the runtime policy; pin the new version with the policy
sync below. `--local-db .local/state.db --apply` writes the same contracts to local
SQLite. Dry-run never creates a database or activates policy.
The history reference cutoff and its actual publication time are retained separately;
runtime reads reject a version published after the requested decision cutoff.
Live hybrid snapshots are cached in the state database's `nba_cache` table, one row per
history version, player, and game, until manager-local midnight. A later pass reuses a row only
when its evidence hash matches: history version, model config, scoring version, target, the
qualified forecast center or its rejection, and an official game report that is fresh at the
cutoff. Within the day this keeps the earlier cutoff's recency decay instead of recomputing it,
and identical snapshots let an unchanged team state skip its replan. Failed projections and
confirmed absences are not cached, and cache read or write failures fall back to projecting.
Hybrid computation has a ten-minute async timeout in queued execution (60 seconds for
inline execution); a timeout leaves every affected projection as an explicit gap.
Due forecast capture runs before planning so that wake's stored revision can be read.
Pre-tipoff planning is due one five-minute Cron interval before the configured move
deadline (15 minutes before tipoff with the default 10-minute lead). This preserves
the movement margin instead of collecting statuses when the deadline has already
arrived. Live collection and the final advice cutoff use completed collection time;
hybrid projection cannot preserve an earlier advice time after consuming that margin.
Lineup and Lock-In messages explain confirmed absences, provisional injury estimates,
and unavailable official reports. Source hashes remain in projection diagnostics.
Visible availability changes revise notification identity; identical refreshed warnings
deduplicate. Recommendation traces retain `availability_notes` and a separate
`availability_material_hash`, preserving the underlying plan's material identity.
Notifications group named statuses by game time and summarize missing reports as
player counts. Lineup and actionable Lock/Pass messages also include their selected
expected terminal weekly team score; unavailable advice labels the estimate as
unavailable. Lock/Pass traces store this as `expected_terminal_score` alongside the
alternative, and selected-score changes participate in material delivery identity.

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

Configure the advice provider in that TOML file:

```toml
[projections]
active_provider = "hybrid" # or "direct_baseline"
hybrid_history_version = "hybrid-runtime-v1-<verified-content-hash>"
```

Hybrid advice requires the explicit published version. The initial history
sync verifies newly selected hybrid artifacts, model, season, full-scoring policy, and
nonempty eligible profiles before activating policy. An omitted `[projections]` table
preserves an existing selection; an explicit table replaces it.

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

Postgame watches start two hours after tipoff and retry every five minutes (15 while the game
is still in progress) until the
opportunity is acknowledged, expired, or otherwise closed. Lock and Pass notifications
include Locked and Passed actions. Unavailable warnings have no notification actions.
Acknowledgement updates recommendation and opportunity state together; a later ESPN
stat correction refreshes the locked score without sending another action.

`--apply` requires `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the environment. Do
not print those values; Wrangler secrets stay in the Worker environment:

- `DISCORD_WEBHOOK_URL`
- `SLEEPER_LEAGUE_ID`
- `SLEEPER_USER_ID`

The production Wrangler configuration declares these secret names as required.
Their values remain encrypted in Cloudflare and must never be added to `[vars]`.
Discord notifications contain the advice and eligible Locked/Passed action links,
without an appended Sleeper navigation link.

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
