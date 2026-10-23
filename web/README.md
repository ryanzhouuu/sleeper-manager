# Web app

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

## Checks

```bash
npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
```
