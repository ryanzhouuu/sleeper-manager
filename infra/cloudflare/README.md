# Cloudflare deployment

This directory contains the state and forecast D1 migrations for the Python Worker. The
canonical Wrangler configuration is `wrangler.toml` at the repository root so
`pywrangler` can discover it from the project root.

The deployed Worker is `locksmith`. D1 databases and the planning queue use the
resource names recorded in that configuration.

## First deployment

From the repository root:

```bash
npx wrangler d1 create sleeper-manager-state
npx wrangler d1 create sleeper-manager-forecast-archive
```

Copy the returned database ID to `wrangler.toml`. Set the Worker URL in
`ACKNOWLEDGEMENT_BASE_URL` as `https://<worker-host>/ack`.

Store these as Worker secrets rather than committing them:

- `DISCORD_WEBHOOK_URL`
- `SLEEPER_LEAGUE_ID`
- `SLEEPER_USER_ID`

The checked-in configuration requires these secret names; their values remain in
Cloudflare. Create a Discord channel webhook and save its URL as an encrypted
`DISCORD_WEBHOOK_URL` secret. Dashboard reminders to synchronize Wrangler configuration
do not require copying secret values into `[vars]`.

Keep `NTFY_TOPIC` unset for Discord-only delivery. If it is configured, ntfy takes
priority and Discord becomes the fallback. `NTFY_ACCESS_TOKEN` is optional for ntfy.
Local commands still accept either destination independently of the Worker configuration.

Apply both schemas and deploy capture only:

```bash
npx wrangler d1 migrations apply sleeper-manager-state --remote
npx wrangler d1 migrations apply sleeper-manager-forecast-archive --remote
uvx --from workers-py pywrangler deploy
```

With no active runtime policy, advisor planning stays blocked and the forecast
collector uses the default 7:00 AM America/Chicago daily schedule. Activate advisor
planning later, after its projection history is ready:

```bash
uv run --extra historical sleeper-manager sync-cloudflare-runtime-data --apply
```

See `docs/cloudflare-runtime.md` for dry-run behavior, required environment variables,
forecast archive checks, and the local `run-scheduled` command.

## HTTP

`GET /health` returns `{"status": "ok"}`. `GET /ack` displays a confirmation form
without changing state. A same-origin `POST /ack` records the action only after an
explicit submission. `GET /home` returns the public roster, matchup, games, and
weekly recommendation document. `/home` does not accept credentials and its body
omits ids, tokens, and secrets.

Python Workers require the `python_workers` compatibility flag and are currently
in beta. Use `uvx --from workers-py pywrangler dev` for local Worker development.


## Manager decision history

Apply `0006_manager_decision_changes.sql` before deploying owner controls. Each
manual correction preserves the previous opportunity state, supersedes outdated
advice and confirmation links, and creates durable planning work in the same D1
transaction. Saved corrections survive a queue wake failure; the next cron can
claim the pending plan. Observed scores and prior acknowledgement rows remain
available for audit.

## Owner controls

The editor lists each player's newest verified, finalized performance while it is
still lockable. Advisor Lock/Pass/Wait and recorded Wait/Locked/Passed are separate:
a recommendation never records a decision. Choose decisions across the cards, then
use the shared **Save & replan** button. Plan and Saved chips distinguish advice
from recorded state; the full recommendation and help stay in expandable details.
Cards reuse the home page's cached ESPN player portraits and NBA team logos.
A stale selection rejects
the entire save; successful saves retain correction history and queue one plan.
Confirmed locks remain visible with correction controls. Expired or forfeited
scores move to history with an automatic outcome and reason.

The [Next.js app](../../web/README.md) serves the dashboard, `/manage` editor, and
`/ack` confirmation pages on one Vercel origin. The Worker retains the same routes
for existing direct links and provides their JSON data to Next.js server routes.
Set a high-entropy `MANAGER_ACCESS_KEY` of at least 32 characters as a Worker secret.
The login form exchanges it for an HttpOnly, Secure, SameSite=Strict cookie scoped
to `/manage`; rotating the secret invalidates existing sessions. Without this
secret the editor returns 503. Public `/home` never includes the key or session.

The existing `/manage` and `/ack` routes also accept `Accept: application/json` for
the Next.js UI. JSON uses the same owner authentication, CSRF, token validation,
version checks, and no-store policy as HTML. Editor documents contain private row
versions, CSRF proofs, and correction history; fetch them only on the server or
through the authenticated same-origin UI. Opening a JSON confirmation is read-only.

Wait leaves the game undecided. Locked confirms a lock already performed in Sleeper.
Pass records the manager's decision not to lock; the player may stay starting or
be substituted, and no Sleeper action is required to record that decision. Locks require a
stable eligible score and the player's currently observed starter slot. A saved
correction preserves prior evidence, expires old confirmation links, and queues a
new plan. Both pages show that the plan is updating while this work is pending.
Plans carry the saved-decision version used during computation; old advice stays
unavailable after a failed or expired job. **Retry plan** queues another attempt
without changing recorded actions. Concurrent decision edits reject obsolete
plan publication at the database boundary.
A plan matching the latest saved-decision version is displayed even when an older
planning job still holds a lease.
The history lists manual corrections; existing notification acknowledgements
remain stored separately. A correction cannot restore a performance forfeited by
a bench transition or an elapsed deadline.

`POST /manage/login`, `/manage/save`, `/manage/replan`, and `/manage/logout` require the same origin.
Form pages use `Referrer-Policy: same-origin` to preserve browser POST origins
while withholding referrers from external sites.
Save/logout also require a session-bound CSRF proof. Submitted league, week and
roster identities are never trusted; scope is resolved from the configured owner.
The editor records decisions only in the assistant's database, not in Sleeper.
