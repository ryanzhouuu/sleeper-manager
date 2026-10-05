# Opening-week projection candidate

The version-one candidate is independent of the active `direct_baseline` advisor.
Its engineering defaults are explicit and model-versioned in `HybridProjectionConfig`.
They are not fitted coefficients or a claim of predictive calibration.

`HybridHistory` separates joint played-game stat evidence from census-derived
`ParticipationOpportunity` records. `JointGameObservation.verified_fields` must come
from source coverage checks: normalized zero values do not establish field presence.
Unresolved identities or outcomes are gaps, not automatic rookie classifications or DNPs.

`HybridHistoryPools` prepares profiles for a cutoff, target season, and scoring policy.
It uses 14-day current-season recency, caps previous-season influence at six games
and older-season influence at three, and adds a five-game donor prior. Donors are
up to 20 other players with at least 10 verified played games, ordered by standardized
seven-stat profile distance and stable player identity. Five donors are required for
pooling; sufficient own history can stand alone. All joint stat lines remain intact.
Uncovered games are exposed through `excluded_games` rather than inferred as zero stats.

`fit_joint_weights` minimizes relative-entropy departure from initial probabilities
subject to core-stat mean intervals. It uses a dependency-free exponential-tilt dual
solver with 200 coordinate iterations. Intervals allow 10% center mismatch with
absolute floors of 1 point, 0.5 rebounds/assists, 0.15 steals/blocks, and 0.25
turnovers/threes. `weight_support` requires ESS at least 20 and individual game mass
at most 10%. These are concentration checks, not predictive calibration. Infeasible
or nonconverged fits return failure evidence for internal fallback.

`HybridParticipation` uses current and previous season census opportunities, including
DNPs, rather than deriving a denominator from archived box scores. A player's rate
shrinks toward other players with 20 opportunity equivalents. A fresh, matching game
report uses a separate observation/status bucket: pooled bucket counts shrink toward
the pooled history rate, and player bucket counts shrink toward that pooled bucket
rate, each at strength 20. Missing reports and submitted-but-not-listed reports remain
distinct. Missing bucket evidence uses history-only output; absent pooled opportunities
block output. These estimates are provisional, not calibrated injury probabilities.

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
method conforms to the existing live projection boundary, but active advice still
uses the current provider. Shadow/replay consumers should retain the batch results.

`build_hybrid_projection_surfaces` takes one or both roster team-week inputs and
creates every remaining player-game at every existing replay cutoff. It batches
shared targets across rosters and retains blockers as failed surface entries. Its
sidecars pass the existing strict artifact codec and can be supplied directly to
`FullAdvisorReplayRequest`. The surface finalization policy describes the existing
team-week replay clock; conditional history uses each supplied observation's own
`finalized_at`. No approximation is silently inserted into hybrid history.
