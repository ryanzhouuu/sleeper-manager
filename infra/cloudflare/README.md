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

Copy the returned database ID to `wrangler.toml`. Optionally set `DASHBOARD_URL`
to the dashboard so notifications include an "Open dashboard" link.

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

`GET /health` returns `{"status": "ok"}`. `GET /home` returns the public roster, matchup, games, and
weekly recommendation document, plus a `lineup` of each player's games with derived
lock states and the planner's decision, and a `plan` summary. `/home` does not accept
credentials; its body omits league and user ids, tokens, and secrets. Lineup rows carry
public Sleeper player ids and ESPN game ids so the owner override can name a game.

Python Workers require the `python_workers` compatibility flag and are currently
in beta. Use `uvx --from workers-py pywrangler dev` for local Worker development.


## Sleeper lock sync

Lock state is read from Sleeper's unauthenticated GraphQL `matchup_legs_raw` query
before every planning run and on every `/home` load; Sleeper wins every conflict
with stored rows. The app never calls a Sleeper mutation or uses a Sleeper login.
The endpoint is undocumented: when it fails or changes shape, `/home` reports
`lock_data: "unavailable"` and plans are marked degraded with no Lock/Pass advice.
The planner decides passes itself; there is no manual Lock or Pass input.

## Owner controls

The only manual input is the tipoff-eligibility override, shown on the dashboard for
an unverified game: "Was starting at tipoff" or "Was benched at tipoff". A Sleeper
lock always outranks the answer. Apply `0006_manager_decision_changes.sql` and
`0007_tipoff_evidence.sql` before deploying; each override is audited, guarded by
the row version, and queues one plan in the same D1 transaction.

Set a non-empty `MANAGER_ACCESS_KEY` as a Worker secret. Any non-empty length is
accepted; a long, randomly generated key is recommended. `POST /manage/login`
exchanges it for an HttpOnly, Secure, SameSite=Strict cookie scoped to `/manage`;
rotating the secret invalidates existing sessions. Without this secret owner
routes return 503. Public `/home` never includes the key or session.

`GET /manage` returns `{"signed_in": bool, "csrf": str|null}`. `POST /manage/login`,
`/manage/logout`, `/manage/replan`, and `/manage/override` require the same origin;
all but login also require the session and its CSRF proof (403 otherwise).
`/manage/override` takes `player_id`, `game_id`, `version`, and `action`
(`lockable` or `not_lockable`) and answers 303, 400, 404, or 409. Submitted league,
week and roster identities are never trusted; scope is resolved from the configured
owner. Responses use `Referrer-Policy: same-origin` and accept
`Accept: application/json` for the Next.js UI.
