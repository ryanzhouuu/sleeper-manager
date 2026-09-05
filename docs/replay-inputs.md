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

The assembler retains coverage and exclusions for incomplete team-weeks. The selected-bundle CLI currently rejects bundles whose exclusions leave no executable player-games. Consumers that need failure accounting should inspect assembler results rather than treating successful CLI output as the sample denominator.

Builder versions `historical-replay-inputs-v2` and `historical-team-week-bundle-v2` fingerprint team observations in the manifest. Eligibility policies use `eligibility-v2` and `observed-weekly-starters-current-catalog-best-known-v2`. Existing bundles are not rewritten. Older artifacts without `inferred_team_membership` remain readable with a default of zero; this compatibility default does not certify that their old opportunity accounting was independently verified. Rebuild them before using the new coverage checks.
