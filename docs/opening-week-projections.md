# Opening-week projection candidate

The version-two candidate is independent of the active `direct_baseline` advisor.
Its engineering defaults are explicit and model-versioned in `HybridProjectionConfig`.
They are not fitted coefficients or a claim of predictive calibration.

`HybridHistory` separates joint played-game stat evidence from census-derived
`ParticipationOpportunity` records. `JointGameObservation.verified_fields` must come
from source coverage checks: normalized zero values do not establish field presence.
Unresolved identities or outcomes are gaps, not automatic rookie classifications or DNPs.

`HybridHistoryPools` prepares profiles for a cutoff, target season, and scoring policy.
It uses 14-day current-season recency, caps opening-night previous-season influence
at six games and older-season influence at three, and adds a five-game donor prior.
The v3 model fades each old-season weight by `strength / (strength + current_games)`
as verified positive-minute, stat-covered current-season games become available.
For a full six-game prior, 24 such current games reduce prior mass to 1.2. Current
recency weights remain unchanged; old outcomes retain positive support. This avoids
a constant old-role prior dominating a current-season weight sum that saturates
under recency decay. Donors are
up to 20 other players with at least 10 verified played games, ordered by standardized
seven-stat profile distance and stable player identity. Five donors are required for
pooling; sufficient own history can stand alone. All joint stat lines remain intact.
Uncovered games are exposed through `excluded_games` rather than inferred as zero stats.

Version two limits sparse own-history concentration before fitting. With qualified
donors, it uniformly reduces own-game weights only as much as needed to keep the
largest normalized own-game weight at the configured 10% cap. Donor selection,
relative own recency weights, and donor raw mass remain unchanged. Supported pools
are unchanged; absent donor support still requires own history to pass all checks.
The external fit and final internal distribution must still pass ESS and maximum
weight checks. This prevents a first observed game from invalidating a supported
donor-based projection without relaxing the concentration limit.

`fit_joint_weights` minimizes relative-entropy departure from initial probabilities
subject to core-stat mean intervals. It uses a dependency-free exponential-tilt dual
solver with 200 coordinate iterations. Intervals allow 10% center mismatch with
absolute floors of 1 point, 0.5 rebounds/assists, 0.15 steals/blocks, and 0.25
turnovers/threes. `weight_support` requires ESS at least 20 and individual game mass
at most 10%. These are concentration checks, not predictive calibration. Infeasible
or nonconverged fits return failure evidence for internal fallback.

`HybridParticipation` uses current and previous season census opportunities, including
DNPs, rather than deriving a denominator from archived box scores. A player's rate
shrinks toward the selected stat-profile donors with 20 opportunity equivalents.
At least five distinct donors must have cutoff-visible census evidence; otherwise
the estimate falls back to all other census players and records the coverage gap.
All verified evidence remains in history, and the player's own census is unchanged.
The trace records the selected pool, opportunity count, and covered player count.
Participation preparation also exposes validated per-player/status census counts.
`HybridParticipation.from_counts` reproduces the same estimator without loading
individual opportunity rows; it requires already cutoff-filtered evidence.
A fresh, matching game report uses a separate observation/status bucket: pooled counts shrink toward
the pooled history rate, and player bucket counts shrink toward that pooled bucket
rate, each at strength 20. Missing reports and submitted-but-not-listed reports remain
distinct. Missing bucket evidence uses history-only output; absent pooled opportunities
block output. These estimates are provisional, not calibrated injury probabilities.

`build_hybrid_runtime_index` prepares the complete donor population and census counts
at an explicit reference cutoff. Current-season mass decays after that reference;
prior-season mass stays fixed. `IndexedHistoryPools` reads only selected players'
exact outcomes while using the full profile population. Backdated reads, mismatched
model configuration, and mismatched scoring policies are rejected. Excluded scoring
fields remain visible. A new history version requires a freshly prepared index.
Runtime artifacts use bounded deterministic compression and content hashes. Typed
index, manifest, and player-partition decoders reject unknown fields, contradictory
counts, and declared stat coverage without actual values.
`HybridRuntimeProjectionProvider` loads one prepared index and one async external
revision for a batch. It fetches only the target and the union of internal/external
donor selections, with at most six concurrent reads, then reuses those outcomes
across that player's games. Full attempt diagnostics remain available in `results`.

Version six distinguishes explicitly confirmed game absences from generic injury
designations. A fresh, matching `GameAvailability(confirmed_absence=True)` requires
a reported `out` status and forces participation to zero even without status-tagged
history. Generic `out` records retain the provisional bucket estimator. Wrong-game,
future, and stale confirmations cannot override historical participation.
When missing or unverified history blocks the conditional played-game forecast,
a confirmed absence still supplies deterministic zero. Its result retains that
`conditional_projection_gap`; no stat line or participation-history rate is invented.
Live status revisions also change projection input identity when their numeric
estimate remains provisional. Generic ESPN designations carry an explicit warning
about unverified game applicability; they cannot force zero participation. Official
source failures remain separate data-gap reasons rather than healthy designations.
`bind_official_game_availability` resolves official report entries against provider
identities and unique schedule team/date/time matches. It preserves submitted-but-not-listed
and missing submissions separately and rejects stale, future, or unhashed evidence.

`HybridProjectionBatch` prepares one history/cutoff/season/scoring-policy combination,
then projects multiple `LiveProjectionTarget`s. Provider identity must be explicitly
resolved. External acceptance requires the supported Sleeper regular-season adapter,
complete coherent core stats, and a successful receipt no older than 48 hours. Provider
update time remains diagnostic; newer failures do not renew freshness. An unsupported
fit independently rebuilds the internal pool. Internal-only configuration has a distinct
model version and does not consume external evidence.

`HybridProjectionResult` retains blocked target keys, source and fallback reasons,
forecast receipts, donor identities, actual and requested stat centers, joint weights,
participation evidence, excluded games, ESS, and maximum weight. Successful snapshots
use the existing league scorer, including nonlinear bonuses and verified foul penalties,
then mix explicit DNP mass at zero. Input fingerprints include visible history and census
evidence; post-cutoff outcomes are excluded. Reuse batches outside scenario loops.

`HybridProjectionProvider(history, archive=...)` reads the existing SQLite forecast
archive by cutoff. `project_batch` accepts all targets on both rosters and optional
availability keyed by `(sleeper_player_id, game_id)`. It prepares each cutoff/season
once and loads one revision for all requested players. An empty archive uses internal
fallback; archive corruption propagates as an error. Its single-target `project`
method conforms to the existing live projection boundary. Runtime policy selects
the active advice provider separately; shadow/replay consumers retain batch results.
`read_player_forecasts_async` uses the same selection rules against D1 and loads
the revision once for all requested players. An empty archive returns no reads;
corruption remains an integrity error rather than ordinary missing evidence.

`build_hybrid_projection_surfaces` takes one or both roster team-week inputs and
creates every remaining player-game at every existing replay cutoff. It batches
shared targets across rosters and retains blockers as failed surface entries. Its
sidecars pass the existing strict artifact codec and can be supplied directly to
`FullAdvisorReplayRequest`. The surface finalization policy describes the existing
team-week replay clock; conditional history uses each supplied observation's own
`finalized_at`. No approximation is silently inserted into hybrid history.

`backtesting.replay.final_roster_text.parse_final_roster_text` extracts explicit
named minute rows, DNP/DND/NWT rows, and inactive names from official final-report
layout text. It does not resolve provider identities or infer DNPs from absence or
zero minutes. Reviewers must bind each unique identity, team side, and outcome to
the retained PDF hash before adding census evidence. Unsupported layouts and
ambiguous inactive names remain coverage gaps; a lone inactive block has no inferred
team side. Expanding the census changes dataset identity, independently of model
coefficient changes.

Run paired local shadow generation with the existing roster artifacts and control
surfaces in the same order:

```bash
uv run python -m sleeper_manager.workflows.hybrid_shadow_cli \
  --history .local/hybrid/history.json \
  --league .local/league.json \
  --team-week .local/replay/own.json .local/replay/opponent.json \
  --control-surface .local/replay/own-control.json .local/replay/opponent-control.json \
  --forecast-archive .local/forecasts.db \
  --output-dir .local/hybrid/shadow
```

Add `--internal-only` for the separately versioned internal comparison, or omit the
archive for ordinary external-missing fallback. Optional `--availability` reads a
JSON list of `{cutoff, player_id, report}` assignments; `report` contains `game_id`,
`observed_at`, `status`, `observation`, and `source_version`. `player_id` here is the
Sleeper ID. Each exact cutoff assignment is checked for game and 15-minute freshness;
absent assignments use history-only participation.

The command writes `roster-<id>-candidate.json` sidecars and `shadow.json`. Each pair
retains the control snapshot or failure and full candidate diagnostics, including
conditional stat centers and DNP probability. Score deltas require matching scoring
policies and cutoff schedules. Candidate failures remain paired records with no
score delta. Repeated identical inputs produce identical artifacts. Sidecars are
immutable; use a new output directory when evidence or model configuration changes.

History JSON has `dataset_version`, `observations`, and `opportunities`, matching
the typed contracts above. IDs in this file are historical provider player IDs.
Every observation contains game/finalization timestamps, participation, minutes,
a `line` with the actual source stat values, `verified_fields`, and `source_version`.
Unknown keys, naive timestamps, string-to-boolean coercions, duplicate records,
and declared coverage for absent stat values are rejected. Opportunities must come
from an independently enumerated census with resolved outcomes. The existing
normalized box-score archive alone cannot supply a verified opportunity denominator
or prove technical/flagrant coverage. Supply that evidence explicitly; gaps are
reported instead of manufacturing a history export from normalized defaults.

`parse_hybrid_history_rows` in `projections.hybrid_history_rows` converts raw
SportsDataverse player-box rows for `season_type == 2`. Supply the validated NBA
schedule, resolved provider player IDs, a source version binding the raw file hash,
and explicit game-keyed `HistoryFinalization` evidence. Each timing record requires
an aware timestamp and source/policy version. Missing timing remains a gap; game end,
retrieval time, and the existing retrospective finalization bound are never substituted
automatically. Callers using a retrospective bound must explicitly supply and label it.

The converter returns played observations, explicit participation outcomes, and gaps.
Absent or null stats remain unverified; present zero counts gain coverage. Missing
minutes remain visible. DNPs supply only participation outcomes. Nonboolean participation,
invalid counts, duplicate player-games, and conflicting NBA teams are rejected.
Technical/flagrant coverage requires actual source fields; play-by-play counts are not
automatically accepted as complete coverage.

`backtesting.replay.final_foul_text.parse_final_foul_text` reads named foul assessments
from complete official final-box layout text. It preserves team sides, technical
subtypes, repeated assessments, flagrant severity, and summary-section presence.
Team and unresolved bench actors remain named events rather than player penalties.
Truncated reports, unknown sections, team contradictions, and inconsistent totals
are rejected. Parsing alone does not resolve player identities or certify omitted
summary sections as verified zeros; those require separate source qualification.
Older separate defensive-three-seconds, delay, excess-timeout, and bench headings
are recognized without treating distinct administrative sections as duplicates.

`qualify_final_game_fouls` requires a complete-summary review covering omitted
sections, a report source version, historical named box-row identities, independent
actor evidence, and explicit finalization timing. Every assessment must reconcile
by name, side, clock, period, and technical subtype. Unmatched or ambiguous actors
reject the game, including conflicts between a nonplayer witness and a box identity.
The resulting per-player counts include reviewed zeros. `attach_hybrid_foul_history`
adds those values and coverage to the original observations, rejects contradictions
with already verified counts, and retains every participation/status record.
Augmented stat evidence becomes visible only after both original and foul evidence
finalization; retrospective timing assumptions must remain explicitly labeled.
`qualify_final_player_fouls` can qualify a specific named played row when an unrelated
actor remains unresolved. Possible surname/initial collisions still require attributed
events, and known target assessments must reconcile by identity, side, clock, and count.
Only the requested player's counts gain coverage; unrelated actors retain their gaps.

`build_hybrid_history` in `projections.hybrid_history_adapter` joins the parsed
sources to reviewed `GameRosterCensus` records. Use `load_final_roster_evidence`
to verify the retained reports and pass both its censuses and source fingerprints.
Only players listed on a participating team enter the expected-opportunity count.
Reviewed players absent from both teams do not become DNPs. A listed player needs
an explicit played/DNP outcome and finalization evidence; unresolved joins remain gaps.

Separately verified inactive evidence can supply `HistoryOutcome` records through
`supplemental_dnp`. Bind each supplement's source version to its reviewed evidence
hash. Played supplements, contradictory outcomes/teams, and duplicate evidence are
rejected. Raw outcomes outside census coverage remain available for stat history,
but never expand the participation denominator.

```python
built = build_hybrid_history(
    (parsed,),
    games=games,
    resolved_player_ids=resolved_player_ids,
    finalization=finalization,
    censuses=reviewed.censuses,
    census_sources=reviewed.source_fingerprints,
    supplemental_dnp=verified_dnp_outcomes,
)
write_hybrid_history_build(Path(".local/hybrid/selected-history"), built)
```

The export writes `history.json` for the existing history loader and `coverage.json`
with source identities, timing policies, declared census scope, counts, and per-record
gaps. Identical reruns preserve bytes; conflicting existing outputs are rejected before
either file is written. Dataset identity binds source values, outcomes, census/cohort
evidence, gaps, and explicit finalization policy. Inspect coverage before claiming
complete history: a scoped census is not a full-season participation inventory.

For full replay, load either generated candidate sidecar using
`load_historical_projection_surface_artifact(path, team_week=week)`, then pass it
as `FullAdvisorReplayRequest.projection_surface` to `run_full_advisor_replay` with
the existing weekly and Lock-In policy configurations. Keep the original
team-week bundle: missing embedded diagnostic projections do not block full replay
when the explicit candidate surface supplies every required cutoff projection.
Full replay validates surface identity, timing, scoring, and completeness before
execution; any failed entry blocks that candidate. Bundle coverage and unrelated
input checks remain intact, and standalone diagnostics still require embedded
projections. Run the control surface with the same policies for a comparison.
The scheduled runtime integration is configured separately as described in
`cloudflare-runtime.md`; selecting hybrid advice requires explicit pinned runtime policy.

To attach historical status buckets without changing the resolved census, run:

```bash
uv run --extra historical python -m sleeper_manager.workflows.hybrid_status_cli \
  --history .local/hybrid/source/history.json \
  --raw-dir .local/model-validation/raw \
  --injury-archive .local/model-validation/injuries \
  --roster-ledger .local/hybrid/source/reviewed-final-game-rosters.json \
  --output-dir .local/hybrid/status-history
```

Repeat `--roster-ledger` for separate seasons. If reviewed players need stable identity
recovery, supply both `--identity-ledger` and `--sleeper-catalog`; these recover names
and provider IDs, never historical teams. Reviewed game censuses supply affiliations.
The default cutoff is 30 minutes before tipoff. The command selects the latest
publication at that cutoff within 24 hours, verifies retained PDF and metadata hashes,
and reparses the PDF. Its AM/PM-free clock must match a unique scheduled team game.
It never resurrects a removed row from an older report.

The separate output retains every original stat observation and played/DNP outcome.
`coverage.json` records tagged buckets, unmatched opportunities, and source hashes.
Historical tags explicitly declare reconstructed publication semantics and retain actual
archive retrieval timestamps; they are not prospective live captures or calibration
evidence. Existing outputs with different content are rejected. Use the resulting
`history.json` as the indexed synchronization input and retain the original coverage
report alongside it. Source gaps, including unsupported scoring fields, stay visible.
