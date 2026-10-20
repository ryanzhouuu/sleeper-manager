# Cloudflare deployment

This directory contains the state and forecast D1 migrations for the Python Worker. The
canonical Wrangler configuration is `wrangler.toml` at the repository root so
`pywrangler` can discover it from the project root.

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

Python Workers require the `python_workers` compatibility flag and are currently
in beta. Use `uvx --from workers-py pywrangler dev` for local Worker development.
