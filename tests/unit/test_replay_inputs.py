from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.backtesting.league_archive import (
    ArchivedRoster,
    HistoricalLeagueArchive,
    PlayerEligibilitySnapshot,
)
from sleeper_manager.backtesting.replay_inputs import (
    HistoricalReplayBuildInput,
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
    ReplayInputError,
    ReplayInputExclusion,
    assemble_historical_team_week_inputs,
    build_replay_input_manifest,
    source_fingerprint,
    write_replay_input_bundle,
)
from sleeper_manager.backtesting.replay_models import ReplayGame, ReplayGameStatus, ReplayPlayerGame
from sleeper_manager.backtesting.roster_timeline import (
    RosterMembershipInterval,
    RosterTimeline,
    build_fantasy_week_boundaries,
)
from sleeper_manager.domain.nba import (
    GameStatus,
    PlayerBoxScore,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.identity import (
    MappingConfidence,
    MappingMethod,
    PlayerMapping,
)

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _archive(scoring: ScoringPolicy) -> HistoricalLeagueArchive:
    return HistoricalLeagueArchive(
        league_id="league-1",
        resolved_from_league_id=None,
        season="2026",
        retrieved_at=NOW,
        scoring_policy=scoring,
        roster_slots=("G",),
        total_rosters=1,
        final_rosters=(),
        matchup_weeks=(),
        transactions=(),
        player_eligibility=(),
        source_artifacts=(),
        configuration_fingerprint="league-config-v1",
    )


def _inputs(
    *,
    scoring: ScoringPolicy | None = None,
    source_bytes: bytes = b"schedule-v1",
    timeline: RosterTimeline | None = None,
) -> HistoricalReplayBuildInput:
    policy = scoring or ScoringPolicy(points=1)
    boundaries = build_fantasy_week_boundaries({1: datetime(2026, 1, 5, tzinfo=UTC).date()})
    timeline = timeline or RosterTimeline("league-1", (), boundaries)
    source = source_fingerprint("raw-schedule", source_bytes)
    return HistoricalReplayBuildInput(
        archive=_archive(policy),
        roster_timeline=timeline,
        week_boundaries=boundaries,
        games=(),
        box_scores=(),
        player_mappings=(),
        scoring_policy=policy,
        source_fingerprints=(source,),
    )


def test_manifest_hash_is_stable_for_identical_inputs() -> None:
    assert (
        build_replay_input_manifest(_inputs()).manifest_id
        == build_replay_input_manifest(_inputs()).manifest_id
    )


def test_changed_scoring_timeline_or_source_changes_manifest() -> None:
    baseline = build_replay_input_manifest(_inputs())
    changed_scoring = build_replay_input_manifest(_inputs(scoring=ScoringPolicy(points=2)))
    changed_source = build_replay_input_manifest(_inputs(source_bytes=b"schedule-v2"))
    interval = RosterMembershipInterval(
        "league-1", 1, "p1", NOW - timedelta(days=1), NOW + timedelta(days=2), ()
    )
    changed_timeline = build_replay_input_manifest(
        _inputs(
            timeline=RosterTimeline(
                "league-1",
                (interval,),
                _inputs().week_boundaries,
            )
        )
    )

    assert baseline.manifest_id != changed_scoring.manifest_id
    assert baseline.manifest_id != changed_source.manifest_id
    assert baseline.manifest_id != changed_timeline.manifest_id


def test_missing_evidence_is_counted_and_exclusion_is_not_complete() -> None:
    coverage = ReplayCoverageSummary(
        expected_player_games=2,
        joined_player_games=1,
        resolved_identities=1,
        exact_eligibility=1,
        best_known_eligibility=0,
        scored_player_games=1,
        missing_evidence=((PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY, 1),),
    )
    result = HistoricalTeamWeekInput(
        manifest_id="manifest-1",
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        starter_slots=("G",),
        roster_player_ids=("p1",),
        observed_starter_ids=("p1",),
        games=(),
        player_games=(),
        eligibility_quality=PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
        coverage=coverage,
        exclusions=(
            ReplayInputExclusion(
                PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                "league-1:week=1:roster=1",
                "Player mapping was ambiguous.",
            ),
        ),
    )

    assert not result.complete
    assert result.to_dict()["coverage"]["missing_evidence"] == [["unresolved_player_identity", 1]]


def test_bundle_writes_versioned_team_week_payload_atomically(tmp_path: Path) -> None:
    manifest = build_replay_input_manifest(_inputs())
    result = HistoricalTeamWeekInput(
        manifest_id=manifest.manifest_id,
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        starter_slots=("G",),
        roster_player_ids=(),
        observed_starter_ids=(),
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
        player_games=(ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 10),),
        eligibility_quality=PlanningQuality.EXACT,
        coverage=ReplayCoverageSummary(1, 1, 1, 1, 0, 1),
    )

    bundle = write_replay_input_bundle(tmp_path / "inputs", manifest, (result,))
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "team-weeks/league-1/week-01/roster-1.json").is_file()
    write_replay_input_bundle(tmp_path / "inputs", manifest, (result,))

    with pytest.raises(ReplayInputError, match="different manifest"):
        write_replay_input_bundle(
            tmp_path / "inputs",
            manifest,
            (replace(result, manifest_id="other"),),
        )


def test_archive_scoring_mismatch_fails_closed() -> None:
    with pytest.raises(ReplayInputError, match="does not match"):
        HistoricalReplayBuildInput(
            archive=_archive(ScoringPolicy(points=1)),
            roster_timeline=RosterTimeline("league-1", (), ()),
            week_boundaries=(),
            games=(),
            box_scores=(),
            player_mappings=(),
            scoring_policy=ScoringPolicy(points=2),
        )


def _source(provider_id: str) -> SourceMetadata:
    return SourceMetadata("fixture", provider_id, NOW)


def _historical_join_inputs() -> HistoricalReplayBuildInput:
    policy = ScoringPolicy(points=1, rebounds=1, turnovers=-1)
    boundaries = build_fantasy_week_boundaries({1: datetime(2026, 1, 5, tzinfo=UTC).date()})
    games = (
        ScheduledGame(
            "g-before-drop",
            datetime(2026, 1, 5, 22, tzinfo=UTC),
            GameStatus.FINAL,
            "home",
            "away",
            None,
            _source("g-before-drop"),
        ),
        ScheduledGame(
            "g-after-reacquisition",
            datetime(2026, 1, 7, 22, tzinfo=UTC),
            GameStatus.FINAL,
            "home",
            "away",
            None,
            _source("g-after-reacquisition"),
            completed_periods=5,
        ),
        ScheduledGame(
            "g-postponed",
            datetime(2026, 1, 8, 22, tzinfo=UTC),
            GameStatus.POSTPONED,
            "home",
            "away",
            "weather",
            _source("g-postponed"),
        ),
        ScheduledGame(
            "g-canceled",
            datetime(2026, 1, 9, 22, tzinfo=UTC),
            GameStatus.CANCELED,
            "home",
            "away",
            "canceled",
            _source("g-canceled"),
        ),
    )
    box_scores = (
        PlayerBoxScore(
            "g-before-drop",
            "provider-p1",
            "home",
            datetime(2026, 1, 5, 22, tzinfo=UTC),
            True,
            False,
            0,
            BoxScoreLine(),
            _source("g-before-drop:provider-p1"),
        ),
        PlayerBoxScore(
            "g-after-reacquisition",
            "provider-p1",
            "home",
            datetime(2026, 1, 7, 22, tzinfo=UTC),
            True,
            True,
            40,
            BoxScoreLine(points=20, rebounds=5, turnovers=2),
            _source("g-after-reacquisition:provider-p1"),
        ),
    )
    timeline = RosterTimeline(
        "league-1",
        (
            RosterMembershipInterval(
                "league-1",
                1,
                "p1",
                datetime(2026, 1, 5, tzinfo=UTC),
                datetime(2026, 1, 6, 12, tzinfo=UTC),
                (),
            ),
            RosterMembershipInterval(
                "league-1",
                1,
                "p1",
                datetime(2026, 1, 7, 12, tzinfo=UTC),
                datetime(2026, 1, 12, tzinfo=UTC),
                ("tx-reacquire",),
            ),
        ),
        boundaries,
    )
    return HistoricalReplayBuildInput(
        archive=replace(
            _archive(policy),
            final_rosters=(ArchivedRoster(1, ("p1",), ("p1",), ()),),
            roster_slots=("G", "BN"),
        ),
        roster_timeline=timeline,
        week_boundaries=boundaries,
        games=games,
        box_scores=box_scores,
        player_mappings=(
            PlayerMapping(
                "p1",
                "provider-p1",
                MappingMethod.STABLE_ID,
                MappingConfidence.HIGH,
                "fixture",
            ),
        ),
        scoring_policy=policy,
        eligibility_evidence=(
            PlayerEligibilitySnapshot(
                "p1", ("G",), datetime(2026, 1, 1, tzinfo=UTC), "fixture", "exact"
            ),
        ),
    )


def test_historical_join_respects_tipoff_membership_and_game_status() -> None:
    result = assemble_historical_team_week_inputs(_historical_join_inputs())

    assert len(result) == 1
    team_week = result[0]
    assert team_week.complete
    assert team_week.eligibility_quality is PlanningQuality.EXACT
    assert [game.game_id for game in team_week.games] == [
        "g-before-drop",
        "g-after-reacquisition",
        "g-postponed",
        "g-canceled",
    ]
    assert [game.game_id for game in team_week.player_games] == [
        "g-before-drop",
        "g-after-reacquisition",
    ]
    assert team_week.player_games[0].actual_score == 0
    assert team_week.player_games[1].actual_score == 23
    assert team_week.player_games[1].membership_segment == "tx-reacquire"
    assert team_week.games[2].status is ReplayGameStatus.POSTPONED
    assert team_week.games[3].status is ReplayGameStatus.CANCELED


def test_unrelated_missing_schedule_does_not_exclude_another_week() -> None:
    base = _historical_join_inputs()
    boundaries = build_fantasy_week_boundaries(
        {
            1: datetime(2026, 1, 5, tzinfo=UTC).date(),
            2: datetime(2026, 1, 12, tzinfo=UTC).date(),
        }
    )
    timeline = replace(
        base.roster_timeline,
        week_boundaries=boundaries,
        intervals=(
            replace(
                base.roster_timeline.intervals[0], ends_at=datetime(2026, 1, 6, 12, tzinfo=UTC)
            ),
            replace(base.roster_timeline.intervals[1], ends_at=datetime(2026, 1, 19, tzinfo=UTC)),
        ),
    )
    orphan = PlayerBoxScore(
        "missing-schedule",
        "provider-p1",
        "home",
        datetime(2026, 1, 14, 22, tzinfo=UTC),
        True,
        True,
        20,
        BoxScoreLine(points=5),
        _source("missing-schedule:provider-p1"),
    )

    result = assemble_historical_team_week_inputs(
        replace(
            base,
            roster_timeline=timeline,
            week_boundaries=boundaries,
            box_scores=base.box_scores + (orphan,),
        )
    )

    week_one = next(team_week for team_week in result if team_week.week == 1)
    week_two = next(team_week for team_week in result if team_week.week == 2)
    assert week_one.complete
    assert not week_two.complete
    assert any(
        exclusion.reason is PlanningReasonCode.MISSING_GAME_SCHEDULE
        for exclusion in week_two.exclusions
    )
