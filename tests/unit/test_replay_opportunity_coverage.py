"""Expected opportunities cannot disappear with missing or conflicting outcome rows."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_replay_inputs import _historical_join_inputs, _projection

from sleeper_manager.backtesting.replay.inputs import (
    HistoricalReplayBuildInput,
    ReplayInputManifest,
    assemble_historical_team_week_inputs,
    build_replay_input_manifest,
)
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode


@pytest.mark.parametrize("remove_all", [False, True])
def test_removing_box_scores_does_not_shrink_expected_opportunities(remove_all: bool) -> None:
    inputs = _historical_join_inputs()
    boxes = () if remove_all else inputs.box_scores[1:]
    result = assemble_historical_team_week_inputs(replace(inputs, box_scores=boxes))[0]

    assert result.coverage.expected_player_games == 2
    assert result.coverage.joined_player_games == len(boxes)
    assert not result.complete
    assert "missing_game_result" in {issue.reason.value for issue in result.exclusions}


def test_exclusion_count_does_not_inflate_expected_player_games() -> None:
    inputs = _historical_join_inputs()
    conflict = replace(inputs.box_scores[0], minutes=1)
    result = assemble_historical_team_week_inputs(
        replace(inputs, box_scores=(*inputs.box_scores, conflict))
    )[0]

    assert result.coverage.expected_player_games == 2
    assert not result.complete


def _with_gap(*, outcome: bool = False) -> HistoricalReplayBuildInput:
    inputs = _historical_join_inputs()
    at = inputs.games[0].start_time + timedelta(hours=12)
    game = replace(inputs.games[0], provider_id="gap", start_time=at)
    boxes = inputs.box_scores
    projections = inputs.projection_snapshots
    if outcome:
        boxes += (replace(inputs.box_scores[0], game_id="gap", played_at=at),)
        projections += (_projection("p1", "gap", inputs.scoring_policy),)
    return replace(
        inputs, games=(*inputs.games, game), box_scores=boxes, projection_snapshots=projections
    )


def test_agreeing_surrounding_records_expose_a_missing_game() -> None:
    result = assemble_historical_team_week_inputs(_with_gap())[0]

    assert result.coverage.expected_player_games == 3
    assert result.coverage.joined_player_games == 2
    assert result.coverage.inferred_team_membership == 1
    assert dict(result.coverage.missing_evidence)[PlanningReasonCode.MISSING_GAME_RESULT] == 1
    assert not result.complete


def test_inferred_membership_is_labeled_even_with_every_outcome_present() -> None:
    result = assemble_historical_team_week_inputs(_with_gap(outcome=True))[0]

    assert result.coverage.expected_player_games == 3
    assert result.coverage.scored_player_games == 3
    assert result.coverage.inferred_team_membership == 1
    assert result.eligibility_quality is PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    assert not result.exclusions
    assert not result.complete
    assert next(game.actual_score for game in result.player_games if game.game_id == "gap") == 0


@pytest.mark.parametrize(
    "history_case", ["absent", "past_only", "future_only", "trade", "conflict"]
)
def test_uncertain_team_history_is_flagged_without_guessing(history_case: str) -> None:
    inputs = _with_gap()
    before, after = inputs.team_observations
    history = {
        "absent": (),
        "past_only": (before,),
        "future_only": (after,),
        "trade": (before, replace(after, team_id="away")),
        "conflict": (before, replace(before, team_id="away"), after),
    }[history_case]
    result = assemble_historical_team_week_inputs(replace(inputs, team_observations=history))[0]

    assert result.coverage.inferred_team_membership == 0
    assert PlanningReasonCode.MISSING_NBA_TEAM_HISTORY in {e.reason for e in result.exclusions}
    assert not result.complete
    assert not result.player_games


def test_expected_games_follow_fantasy_membership_at_tipoff() -> None:
    inputs = _historical_join_inputs()
    first, second = inputs.roster_timeline.intervals
    other = replace(second, roster_id=2)
    timeline = replace(inputs.roster_timeline, intervals=(first, other))
    results = assemble_historical_team_week_inputs(replace(inputs, roster_timeline=timeline))

    assert {row.roster_id: row.coverage.expected_player_games for row in results} == {1: 1, 2: 1}
    assert all(row.complete for row in results)
    assert {row.roster_id: row.player_games[0].game_id for row in results} == {
        1: "g-before-drop",
        2: "g-after-reacquisition",
    }


def test_off_roster_and_other_nba_team_games_are_not_expected() -> None:
    inputs = _with_gap()
    gap = inputs.games[-1]
    off_roster = replace(gap, start_time=gap.start_time + timedelta(hours=10))
    unrelated = replace(gap, provider_id="other", home_team_id="third", away_team_id="fourth")
    result = assemble_historical_team_week_inputs(
        replace(inputs, games=(*inputs.games[:-1], off_roster, unrelated))
    )[0]

    assert result.coverage.expected_player_games == 2
    assert result.complete


def test_nonfinal_box_score_is_not_a_completed_result() -> None:
    inputs = _historical_join_inputs()
    active = replace(inputs.games[0], status=GameStatus.IN_PROGRESS)
    result = assemble_historical_team_week_inputs(
        replace(inputs, games=(active, *inputs.games[1:]))
    )[0]

    assert result.coverage.expected_player_games == 2
    assert result.coverage.scored_player_games == 1
    assert PlanningReasonCode.MISSING_GAME_RESULT in {e.reason for e in result.exclusions}
    assert not result.complete


def test_duplicate_observations_and_schedules_do_not_inflate_inventory() -> None:
    inputs = _with_gap(outcome=True)
    result = assemble_historical_team_week_inputs(
        replace(
            inputs,
            team_observations=inputs.team_observations * 2,
            games=inputs.games * 2,
        )
    )[0]

    assert result.coverage.expected_player_games == 3
    assert result.coverage.inferred_team_membership == 1
    assert not result.exclusions


def test_team_observations_are_fingerprinted_separately_from_outcomes() -> None:
    inputs = _historical_join_inputs()
    original = build_replay_input_manifest(inputs)
    missing = build_replay_input_manifest(replace(inputs, team_observations=()))
    no_outcomes = build_replay_input_manifest(replace(inputs, box_scores=()))

    def hashes(manifest: ReplayInputManifest) -> dict[str, str]:
        return {item.name: item.content_hash for item in manifest.source_fingerprints}

    assert original.manifest_id != missing.manifest_id
    assert (
        hashes(original)["player-team-observations"]
        == hashes(no_outcomes)["player-team-observations"]
    )
    assert hashes(original)["parsed-box-scores"] != hashes(no_outcomes)["parsed-box-scores"]


def test_unresolved_identity_is_not_counted_twice_by_the_inventory_and_join() -> None:
    inputs = _historical_join_inputs()
    unresolved = replace(inputs.player_mappings[0], espn_id=None, reason="Absent player")
    result = assemble_historical_team_week_inputs(
        replace(inputs, player_mappings=(unresolved,), box_scores=())
    )[0]

    assert result.coverage.missing_evidence == ((PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY, 1),)
    assert result.coverage.expected_player_games == 0
    assert not result.complete


def test_direct_proxy_observations_never_claim_exact_complete_membership() -> None:
    inputs = _historical_join_inputs()
    assert assemble_historical_team_week_inputs(inputs)[0].complete
    approximate = replace(
        inputs,
        team_observations=tuple(
            replace(observation, approximate=True) for observation in inputs.team_observations
        ),
    )
    result = assemble_historical_team_week_inputs(approximate)[0]
    assert result.coverage.expected_player_games == result.coverage.joined_player_games == 2
    assert result.coverage.inferred_team_membership == 2
    assert not result.complete
    assert (
        build_replay_input_manifest(inputs).manifest_id
        != build_replay_input_manifest(approximate).manifest_id
    )
