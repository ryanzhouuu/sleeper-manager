# Web app

The dashboard (`/`), owner decision editor (`/manage`), and notification review
(`/ack`) deploy together as one Next.js app. They share the
dashboard's dark monospace UI. Saving shows progress in place; pending plans refresh
for up to one minute, pausing while choices are edited. A stale save refreshes current
decisions and asks for another review rather than resubmitting rejected choices.

The Next.js app uses the Python Cloudflare Worker for league data and decision
validation. Set server-only `HOME_URL` to `https://<worker-host>/home`. Never use a
`NEXT_PUBLIC_` variable for owner credentials or Worker secrets.

Private reads use `GET /manage/api`. Owner writes use
`POST /manage/api/{login,save,replan,logout}`; scoped notification confirmations use
`POST /ack/submit`. These fixed server routes validate browser POST origins, forward
only the owner session cookie and bounded URL-encoded forms, and request JSON from
the Worker. Private responses are never cached. Worker sessions, CSRF proofs,
decision versions, and explicit confirmation requirements remain authoritative.

Deploy the updated Worker before using the new private endpoints. Existing direct
Worker HTML links remain compatible.

## Deploy to Vercel

1. Deploy the updated Python Worker from the repository root using
   `uvx --from workers-py pywrangler deploy`. It must support the JSON control
   documents before the new UI is used. Keep the current D1 databases, planning
   queue, schedules, and Worker secrets.
2. Import this repository into Vercel. Select **Next.js** and set **Root Directory**
   to **`web`**. The default install (`npm ci`) and build (`npm run build`) use
   `web/package-lock.json` and `web/vercel.json`.
3. Add `HOME_URL=https://<worker-host>/home` to the Vercel project's server
   environment. Set it for Production and only the Preview/Development environments
   that should access that league. Owner access still requires the Worker's existing
   `MANAGER_ACCESS_KEY`; do not copy it, league IDs, or notification secrets to Vercel.
4. Deploy the Vercel project and sign in at `https://<app-host>/manage` with the
   existing owner access key. The browser receives a new signed session cookie for
   the app's origin; a previous Worker-origin session does not carry over.
5. Set the Worker's `ACKNOWLEDGEMENT_BASE_URL` to `https://<app-host>/ack` in the
   canonical Wrangler configuration and redeploy the Worker. New notifications then
   open the Next.js confirmation page. Existing notification URLs still work on the
   Worker. Do not run the planner on Vercel or configure duplicate cron jobs.

No database migrations or new paid services are required for this UI consolidation.
The app server forwards authenticated traffic, so no browser CORS configuration or
cross-site cookies are needed. Preview deployments use their own origin-scoped
owner sessions; submitting a decision there still affects the configured league.

## Local development

Copy `.env.example` to ignored `.env.local` and set `HOME_URL` to the updated Worker
or a local Worker instance, then run `npm ci` and `npm run dev`. Open `/manage` to
sign in. Passwords and notification tokens are submitted with POST and never added
to navigation URLs by forms. Notification review URLs themselves contain scoped
tokens, so avoid copying them into logs or committed fixtures.

## Checks

```bash
npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
```
