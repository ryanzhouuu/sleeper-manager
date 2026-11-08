# Web app

The dashboard (`/`) is a single page: the live lock-in score for both sides, the
plan's projected team total before and after its advice, each lineup slot with this
week's games and their lock states, and the planner's decision on the row it affects.
Lock state comes from Sleeper; the only manual input is the tipoff-eligibility
override shown on an unverified game, which asks for the owner access key once per
session.

The Next.js app reads the Python Cloudflare Worker. Set server-only `HOME_URL` to
`https://<worker-host>/home`. Never use a `NEXT_PUBLIC_` variable for owner
credentials or Worker secrets.

`GET /manage/api` reads the owner session. Owner writes use
`POST /manage/api/{login,override,replan,logout}`. These fixed server routes validate
browser POST origins, forward only the owner session cookie and bounded URL-encoded
forms, and request JSON from the Worker. Private responses are never cached. Worker
sessions, CSRF proofs, and row versions remain authoritative.

Deploy the updated Worker before the updated web app.

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
4. Deploy the Vercel project. The dashboard asks for the existing owner access key
   the first time an unverified game is answered; the signed session cookie is
   scoped to the app's origin.
5. Optionally set the Worker's `DASHBOARD_URL` to `https://<app-host>/` and redeploy
   the Worker so notifications link to the dashboard. Do not run the planner on
   Vercel or configure duplicate cron jobs.

No database migrations or new paid services are required for this UI consolidation.
The app server forwards authenticated traffic, so no browser CORS configuration or
cross-site cookies are needed. Preview deployments use their own origin-scoped
owner sessions; submitting a decision there still affects the configured league.

## Local development

Copy `.env.example` to ignored `.env.local` and set `HOME_URL` to the updated Worker
or a local Worker instance, then run `npm ci` and `npm run dev`. The access key is
submitted with POST and never added to navigation URLs.

## Checks

```bash
npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
```
