# Sleeper Manager

Personal, read-only fantasy basketball assistant for Sleeper Lock-In leagues. It
combines Sleeper league data with NBA schedules, availability, and projections to
recommend weekly lineup and Lock-In decisions. Optional ntfy or Discord
notifications; it never submits roster changes to Sleeper.

See [LOCK_IN_MODE.md](LOCK_IN_MODE.md) for the league rules the decision engine
enforces.

## Local setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --all-groups
```

Create an ignored `.env`:

```dotenv
SLEEPER_LEAGUE_ID=your-league-id
SLEEPER_USER_ID=your-user-id
```

Optional manager policy (defaults work without one):

```bash
mkdir -p .local
cp manager-policy.example.toml .local/policy.toml
```

```bash
uv run sleeper-manager check-config
uv run sleeper-manager bootstrap
uv run sleeper-manager check-nba-data
```

Local commands persist to SQLite at `.local/state.db` by default.

## Configuration

Settings load from environment variables or `.env`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `SLEEPER_LEAGUE_ID` | League for live commands | Required |
| `SLEEPER_USER_ID` | Manager roster identity | Required |
| `TIMEZONE` | Display and scheduling timezone | `America/Chicago` |
| `MANAGER_POLICY_PATH` | Manager policy TOML | `.local/policy.toml` |
| `SQLITE_PATH` | Local state database | `.local/state.db` |
| `NTFY_TOPIC` | Enables ntfy delivery | Empty |
| `NTFY_BASE_URL` | ntfy service root | `https://ntfy.sh` |
| `NTFY_ACCESS_TOKEN` | Optional ntfy auth | Empty |
| `DISCORD_WEBHOOK_URL` | Enables Discord delivery | Empty |
| `ACKNOWLEDGEMENT_BASE_URL` | Notification action callbacks | Empty |

Notification commands need `NTFY_TOPIC` or `DISCORD_WEBHOOK_URL`. Interactive
notifications also need `ACKNOWLEDGEMENT_BASE_URL`.
The checked-in Worker configuration requires Discord; store its webhook URL as a
Cloudflare secret as described in [Cloudflare deployment](infra/cloudflare/README.md).

## Commands

```bash
uv run sleeper-manager --help
```

| Command | Purpose |
| --- | --- |
| `check-config` | Report sanitized configuration readiness |
| `bootstrap` | Validate and summarize the configured league |
| `check-nba-data` | Report NBA provider health and mapping coverage |
| `check-forecast-capture` | Report local forecast archive health |
| `test-notification` | Send one local notification diagnostic |
| `run-scheduled` | Run one local due-work wake against SQLite |

## Forecast capture

The [opening-week hybrid candidate](docs/opening-week-projections.md) uses captured
forecasts with internal fallback for paired shadow output and full-advisor replay.
The [scheduled runtime](docs/cloudflare-runtime.md) supports pinned D1 hybrid history
and batched shadow evaluation independently from the selected advice provider.
Official game absences constrain active projections; uncertain or unverified status
evidence is explained in lineup and Lock-In messages. Existing policies use direct-baseline
advice; hybrid advice requires an explicit selection and pinned history.

## Recommendations

Notifications group named availability warnings by status and game time, and count
players with missing reports or unverified availability. Full player/game warnings
remain in the stored recommendation trace.
Daily, pre-tipoff, and postgame runs produce a complete team recommendation from
the current lineup, confirmed decisions, and retained player-game observations.
Alerts lead with the deadline and ordered actions, followed by the projected weekly
total, lineup gain, confidence, and essential warnings. Recovery instructions name
only changed slots and retain the review deadline and forfeiture consequences.
Full lineups, bench choices, confirmed decisions, placement comparisons, and modeling
details remain in the stored recommendation trace.
Locked confirms a lock already made in Sleeper; Pass records
the manager's decision not to lock that game. Passing allows the planner to substitute
the player without requiring a substitution. Delivering advice never records a
decision or proves that a lineup move or lock occurred. Discord links
and ntfy confirmation buttons open a review screen; only submitting its form records
a decision. The dashboard
links to owner-only controls for recording or correcting each player/game's
Lock/Pass status. The editor shows each player's latest finalized, still-lockable game,
with the advisor's recommendation separate from the recorded Wait/Locked/Passed state.
Wait leaves a game undecided. Select decisions across games and save once to queue
one plan for the complete set with **Save & replan**. Compact icon controls keep
Plan and Saved states separate; full advice and help are expandable.
Expired or forfeited performances move to history with their reason; confirmed locks
remain visible. Saving retains correction history and queues a fresh plan; see
[owner controls](infra/cloudflare/README.md#owner-controls) for access setup.

The Next.js dashboard, owner editor, and notification confirmation pages deploy
together on Vercel; Python planning, scheduling, and persistence stay on Cloudflare.
See [web deployment](web/README.md) for the project root, environment variable, and
notification callback setup.

Stable completed scores remain available for joint comparison with later games.
Observed bench transitions permanently forfeit that performance; returning the player
to a starting slot does not restore it. An extra attempt is preferred at equal
modeled value only when an executable recovery preserves the baseline choices.
Recovery obligations persist across runs, including after a recovery recommendation
is ignored. A named recovery may explicitly forfeit its own active attempt; it
cannot discard another active starter or relocate an accepted lock.

Completed-score comparisons evaluate every placement accepted by move verification
and every distinct continuation, using shared scenario draws and an approximate
later-week continuation. Duplicate player/slot combinations are pruned before
evaluation. Move-verification limits, runtime limits, or uncertain evidence can
still make advice or totals unavailable. These changes do not claim a weekly
scoring improvement or automatic lock detection.

Large deliveries use ordered message parts to preserve the whole
recommendation and its confirmations within [Discord's content limit](https://docs.discord.com/developers/resources/webhook#execute-webhook)
and [ntfy's message/action limits](https://docs.ntfy.sh/publish/#limitations).
Scheduled daily runs send one confirmation per manager-local day when no changes
are needed, including availability warnings. Unchanged pre-tipoff results stay quiet.
A failed multipart delivery retries as a whole, so already received parts can repeat
after a partial transport failure.

Lineup and Lock/Pass messages show the expected weekly team total assuming the
recommendation is followed, including locked scores and modeled remaining slots.
Lineups show the change versus the current lineup; Lock/Pass messages show the other
action's team total separately from the completed player's score. Blocked advice or
missing estimates display `Projected week: unavailable.` in lineup alerts.

## Forecast archive

`run-scheduled` writes forecast evidence to `forecasts.db` beside `SQLITE_PATH`
(`.local/forecasts.db` by default). That write does not change the planning
exit status or lineup and Lock-In advice.

`check-forecast-capture` reads that file. It reports the newest capture, UTC-day
request and write counts, gaps from failed, invalid, skipped, or late attempts,
and encoded payload bytes against a 350 MB alert. A missing file means nothing
has been recorded yet; the command does not create one. The 350 MB figure is the
sum of stored payload bytes, not a database file size.

The Worker stores forecast captures in the separate `forecast_archive` D1 binding.
Each receipt can include the roster and schedule snapshot read at capture time.
Apply its migrations before deploying; see [Cloudflare runtime](docs/cloudflare-runtime.md)
for deployment and remote archive checks. Keep this archive out of
`sleeper_manager_state`. Without an active runtime policy, the Worker captures at
7:00 AM America/Chicago while advisor planning stays blocked.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=sleeper_manager --cov-report=term-missing
```
