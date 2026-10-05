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
