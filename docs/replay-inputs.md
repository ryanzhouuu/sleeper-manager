# Historical replay input evidence

Historical bundle construction keeps expected opportunities separate from available game results. `HistoricalReplayBuildInput.team_observations` contains attributable `PlayerTeamObservation` records (provider player ID, NBA team ID, timezone-aware observation time and source). Callers must provide the complete selected-week schedule and freeze membership evidence separately from selected outcomes.

The bundle builder extracts team observations from season-wide historical player records. This is not an independently acquired NBA roster archive: missing historical rows can leave membership unknown. At an observation time, conflicting team records are rejected. Between observations, membership is inferred only when the nearest records on both sides agree. The builder does not extrapolate beyond history endpoints or bridge conflicting team records. Future surrounding records support labeled retrospective reconstruction only; their scores and other future outcomes are not policy inputs.

Expected opportunities follow fantasy-roster membership at tipoff and the NBA schedule. Postponed and canceled games do not require a result. Other expected games need a final box score, including an explicit zero-valued DNP record where appropriate. An absent row is not treated as a DNP.

Coverage fields:

- `expected_player_games`: known rostered opportunities from the independent enumeration, not the number of box scores or error messages. Where membership is unknown, this is a known count rather than a complete total; the missing-evidence flags prevent a completeness claim.
- `joined_player_games`: available joined outcome rows, which may be unusable when evidence conflicts.
- `resolved_identities`: expected opportunities with an unambiguous player mapping.
- `inferred_team_membership`: expected opportunities whose NBA membership was inferred between agreeing records. Nonzero counts prevent strict `complete` status even when all outcomes exist.
- `missing_game_result`: an expected outcome is absent or its game is not final.
- `missing_nba_team_history`: membership is unavailable or ambiguous, or a joined box score disagrees with the independently expected team.

Eligibility is exact only when selected snapshots are available by the cutoff, explicitly labeled `exact`, attributed to a source, and consistent at that timestamp. Current-catalog or proxy snapshots remain best-known even when timestamped before a game. Conflicting tied snapshots fail closed, including future fallbacks.

The assembler retains coverage and exclusions for incomplete team-weeks. The selected-bundle builder persists these immutable artifacts before rejecting bundles with no executable player-games. It raises `HistoricalTeamWeekAssemblyError`, a `HistoricalTeamWeekBundleError` subclass whose `output` contains the manifest, team-week and retained paths. The CLI still fails for these inputs. Earlier source failures can occur before a bundle exists; consumers must retain those failed attempts separately. Successful CLI output alone is never the sample denominator.

Builder versions `historical-replay-inputs-v2` and `historical-team-week-bundle-v2` fingerprint team observations in the manifest. Eligibility policies use `eligibility-v2` and `observed-weekly-starters-current-catalog-best-known-v2`. Existing bundles are not rewritten. Older artifacts without `inferred_team_membership` remain readable with a default of zero; this compatibility default does not certify that their old opportunity accounting was independently verified. Rebuild them before using the new coverage checks.

## Reviewed final inactive outcomes

The bundle API accepts an optional `inactive_evidence_path`; its CLI equivalent is
`--inactive-evidence /path/to/reviewed.json`. Without it, no supplemental outcome is
loaded. The supplied JSON uses `schema_version: "reviewed-final-inactive-v1"` and
an array named `records`. Each record requires:

| Field | Meaning |
| --- | --- |
| `player_id`, `player_name` | Existing NBA provider ID and matching full name |
| `game_id`, `team_id`, `game_start` | Existing final game, participating NBA team and exact timezone-aware scheduled tipoff |
| `status` | Literal `confirmed_final_inactive` |
| `pdf_path`, `pdf_sha256` | Retained official final PDF and its SHA-256 hash; relative paths resolve beside the ledger |
| `report_url`, `page` | Official `https://statsdmz.nba.com/pdfs/YYYYMMDD/YYYYMMDD_AWAYHOME.pdf` source and positive page number |
| `retrieved_at`, `verification` | Timezone-aware retrieval time and nonempty explanation of the identity/game/status review |

This is a reviewed-data import boundary, not an automatic PDF parser. The reviewer
must verify the full player identity, team, game and final inactive listing in the
PDF. The importer checks the schema, identities, game finality, tipoff, team,
report date, retained PDF signature/hash and conflicting existing results. It
rejects duplicate player-games, PDFs assigned to different games, missing files
and contradictory outcomes. It never substitutes a pregame Out report or absence
of a box score for confirmation. Compatible existing zero/DNP rows retain their
original source plus the final report attribution.

The bundle fingerprints the complete ledger and supporting PDF hashes. Only
selected roster/week outcomes are merged for scoring; each new outcome has zero
statistics and `did_play=False`. Supplements do not enter team-history observations
or projection training history. They can request the existing pregame projection
for a recovered target using the original historical inputs. Thus membership
uncertainty and approximate finalization/eligibility labels remain unchanged, and
unresolved team-weeks can still fail assembly after scoring recovery.

`historical-team-week-bundle-v3` identifies this integration. Prior immutable
bundles remain intact. A supplied ledger is validated in full against the loaded
NBA inputs, so callers should select a ledger for those seasons. This mechanism
supports bounded reviewed recovery; it does not certify broad historical coverage.

## Supplemental provider identities

`--identity-evidence /path/to/sources.json` (API `identity_evidence_path`) can
recover otherwise unresolved player IDs from retained ESPN roster responses.
The source ledger has `schema_version: "espn-roster-identities-v1"` and `sources`,
whose entries contain `path`, `url`, `retrieved_at` and `sha256`. Paths are relative
to the ledger unless absolute; URLs identify ESPN's NBA `/teams/{id}/roster`
endpoint. Raw response bytes and the ledger are fingerprinted.

Recovery requires a unique provider ID matching the cached Sleeper full name and
birth date. Missing dates, conflicting identities and ambiguous matches remain
unresolved; invalid schemas or changed source hashes raise an error. Only
unresolved mappings are eligible. Current team and active-state fields are
removed from recovered identities, so current rosters do not establish historical
membership or add outcome/training data. Recovered IDs can also validate explicitly
supplied final-inactive evidence. `historical-team-week-bundle-v4` identifies this
additional evidence path; all prior immutable artifacts remain intact.

## Injury-report team proxies

The optional `--injury-team-evidence /path/to/reports.json` flag (API
`injury_team_evidence_path`) accepts a ledger with
`schema_version: "injury-team-proxies-v1"` and `reports`, an array of parsed-cache
`path` / `sha256` objects. Each version-2 injury cache must retain its neighboring
`.pdf`; the loader verifies both cache and PDF hashes and publication metadata.
All selected input hashes and the selection policy enter the manifest.

For each completed game and team, select the latest supplied team report published
no later than scheduled tipoff. Match the schedule by Eastern game date and both
teams; ambiguous schedule matches are rejected. A player omitted by that report
is not carried forward from an older one. Tied reports must agree on the player
association, names must resolve to one provider ID, and `Trade Pending` entries
are excluded. An unsubmitted team report supplies no observation. This policy
means latest **among supplied reports**; the ledger must contain the intended
report window, and it does not certify that every historical publication was
captured.

Each supported association is a `PlayerTeamObservation(approximate=True)` at that
game's scheduled tipoff. This is an explicit retrospective proxy, not a claim of
exact legal affiliation. The existing agreeing-neighbor rule still determines
membership between observations and still flags trade gaps and endpoints.
`inferred_team_membership` also counts direct approximate observations, including
when an agreeing exact observation exists at the same time. Such evidence cannot
produce strict completeness. The approximation flag defaults to false for
existing direct records and is fingerprinted. Versions
`historical-replay-inputs-v3` and `historical-team-week-bundle-v5` identify this
provenance-aware behavior.

Team proxies do not create zero outcomes, projections, or training observations.
Newly established opportunities can expose more missing game results; those need
independent final outcome evidence before scoring. Pre-tipoff availability data
used by an eventual policy must separately respect its actual decision cutoff.

## Complete sample accounting

Use the [experiment input index](experiment-input-index.md) to select immutable
bundles explicitly and retain all declared team-weeks, including unprocessed
keys and failed inputs. `HistoricalTeamWeekAssemblyError.output` supplies the
retained bundle references for assembly failures. Errors before assembly need a
separate failed-attempt evidence record. Input assembly counts and strict evidence
quality remain separate from full-advisor replay readiness.
