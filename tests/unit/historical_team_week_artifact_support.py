"""Shared canonical fixtures for historical team-week artifact tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sleeper_manager.backtesting.artifacts import canonical_json_bytes
from sleeper_manager.backtesting.replay.inputs import (
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
    ReplayInputExclusion,
    ReplayInputManifest,
    SourceFingerprint,
)
from sleeper_manager.backtesting.replay.models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
)
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.projection import (
    ProjectionAdjustmentKind,
    ProjectionComponent,
    ProjectionDistribution,
    ProjectionFallback,
    ProjectionReason,
    ProjectionSnapshot,
)

NOW = datetime(2026, 2, 2, 19, 30, tzinfo=UTC)


def _write_lone_payload(root: Path, team_week: HistoricalTeamWeekInput) -> Path:
    path = root / "roster.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(team_week.to_dict()))
    return path


def _manifest() -> ReplayInputManifest:
    return ReplayInputManifest(
        league_id="league-1",
        season="2026",
        builder_version="historical-team-week-bundle-v1",
        source_fingerprints=(SourceFingerprint("fixture", "abc123"),),
        scoring_policy_version="scoring-v1",
        scoring_policy_fingerprint="policy-fp",
        league_configuration_fingerprint="league-fp",
        roster_timeline_fingerprint="roster-fp",
        week_boundaries_fingerprint="week-fp",
        eligibility_policy_version="eligibility-v1",
        projection_config_version="projection-v1",
    )


def _team_week(manifest_id: str) -> HistoricalTeamWeekInput:
    projection = ProjectionSnapshot(
        player_id="p1",
        game_id="g1",
        available_as_of=NOW,
        model_version="projection-v1",
        input_version="inputs-v1",
        scoring_policy_version="scoring-v1",
        distribution=ProjectionDistribution.from_weighted_observations(((10.0, 1.0),)),
        reasons=(ProjectionReason("recency", "Used prior games.", adjustment=None),),
        components=(
            ProjectionComponent(
                code="base",
                estimate=10.0,
                baseline=9.0,
                adjustment=1.0,
                kind=ProjectionAdjustmentKind.ADDITIVE,
                effective_sample=4.0,
                fallback=ProjectionFallback.OBSERVED,
                message="Observed prior games.",
            ),
        ),
    )
    return HistoricalTeamWeekInput(
        manifest_id=manifest_id,
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        starter_slots=("G",),
        roster_player_ids=("p1",),
        observed_starter_ids=("p1",),
        games=(
            ReplayGame(
                "g1",
                NOW,
                NOW + timedelta(hours=2),
                1,
                ("home", "away"),
                ReplayGameStatus.FINAL,
            ),
        ),
        player_games=(
            ReplayPlayerGame(
                "p1",
                "provider-p1",
                "g1",
                1,
                True,
                ("PG",),
                10.0,
                projection,
                "segment-1",
            ),
        ),
        eligibility_quality=PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
        coverage=ReplayCoverageSummary(
            expected_player_games=1,
            joined_player_games=1,
            resolved_identities=1,
            exact_eligibility=0,
            best_known_eligibility=1,
            scored_player_games=1,
            missing_evidence=((PlanningReasonCode.AMBIGUOUS_ELIGIBILITY, 1),),
            projected_player_games=1,
        ),
        exclusions=(
            ReplayInputExclusion(
                PlanningReasonCode.AMBIGUOUS_ELIGIBILITY,
                "league-1:week=1:roster=1",
                "Exact historical eligibility was unavailable.",
            ),
        ),
    )
