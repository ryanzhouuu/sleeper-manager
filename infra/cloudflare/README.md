# Cloudflare deployment

This directory contains the D1 migration for the Phase 3 Python Worker. The
canonical Wrangler configuration is `wrangler.toml` at the repository root so
`pywrangler` can discover it from the project root.

## First deployment

From the repository root:

```bash
npx wrangler d1 create sleeper-manager-state
```

Copy the returned database ID to `wrangler.toml`. Set the Worker URL in
`ACKNOWLEDGEMENT_BASE_URL` as `https://<worker-host>/ack`.

Store these as Worker secrets rather than committing them:

- `NTFY_TOPIC`
- `NTFY_ACCESS_TOKEN` (optional)
- `DISCORD_WEBHOOK_URL` (optional)
- `SLEEPER_LEAGUE_ID`
- `SLEEPER_USER_ID`

Apply the schema and deploy:

```bash
npx wrangler d1 migrations apply sleeper-manager-state --remote
uvx --from workers-py pywrangler deploy
```

Python Workers require the `python_workers` compatibility flag and are currently
in beta. Use `uvx --from workers-py pywrangler dev` for local Worker development.
