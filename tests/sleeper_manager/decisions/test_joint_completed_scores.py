"""Joint lock placement uses actual roster value and executable complete lineups."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions import completed_scores
from sleeper_manager.decisions._coordinated_continuations import coordinate_completed_continuations
from sleeper_manager.decisions.completed_scores import compare_completed_scores
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PlanningGameStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state


def _completed(player, score, positions=("PG",), indices=(0, 1, 2)):
    return _opportunity(
        player,
        f"done-{player}",
        NOW - timedelta(hours=3),
        positions,
        ((score, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=score,
        finalized_at=NOW - timedelta(minutes=1),
        eligible_slot_indices=indices,
    )


def test_guard_locks_at_pg_to_preserve_two_centers() -> None:
    guard = _completed("guard", 30)
    centers = tuple(
        _opportunity(
            player,
            player,
            NOW + timedelta(days=1),
            ("C",),
            ((score, 1),),
            eligible_slot_indices=(1, 2),
        )
        for player, score in (("c1", 60), ("c2", 50))
    )
    state = replace(
        _state(
            (guard, *centers),
            starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "C"), StarterSlot(2, "UTIL")),
            observed=(ObservedStarter(2, "guard", ("PG",)),),
        ),
        bench_capacity=2,
    )
    result = compare_completed_scores(state, scenario_count=8)
    assert [(row.player_id, row.slot_index) for row in result.selected.locks] == [("guard", 0)]
    assert result.selected.expected_score == 140
    assert result.selected.moves
    alternative = next(
        item for item in result.alternatives if item.locks and item.locks[0].slot_index == 2
    )
    assert alternative.expected_score == 90


def test_remaining_roster_can_make_util_the_better_position() -> None:
    guard = _completed("guard", 30)
    other = _opportunity(
        "other", "later", NOW + timedelta(days=1), ("PG",), ((70, 1),), eligible_slot_indices=(0,)
    )
    state = replace(
        _state(
            (guard, other),
            starter_slots=(StarterSlot(0, "PG"), StarterSlot(2, "UTIL")),
            observed=(ObservedStarter(0, "guard", ("PG",)),),
        ),
        bench_capacity=1,
    )
    result = compare_completed_scores(state, scenario_count=8)
    assert result.selected.locks[0].slot_index == 2
    assert result.selected.expected_score == 100


def test_joint_acceptance_beats_a_conflicting_greedy_lock() -> None:
    a, b = _completed("a", 60, indices=(1,)), _completed("b", 60, indices=(0,))
    later = _opportunity(
        "a", "future", NOW + timedelta(days=1), ("PG",), ((100, 1),), eligible_slot_indices=(0,)
    )
    state = replace(
        _state(
            (a, b, later),
            observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
        ),
        bench_capacity=1,
    )
    result = compare_completed_scores(state, scenario_count=8)
    assert {row.player_id for row in result.selected.locks} == {"a", "b"}
    assert result.selected.expected_score == 120
    assert result.baseline.expected_score == 100


def test_fixed_slots_cannot_be_relocated_by_joint_search() -> None:
    a, b = _completed("a", 20, indices=(0, 1)), _completed("b", 60, indices=(0, 1))
    fixed = FixedSlot(0, "G", "a", a.game_id, 20, NOW, "confirmed", "fixture")
    state = _state(
        (a, b),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
        fixed=(fixed,),
    )
    result = compare_completed_scores(state, scenario_count=8)
    assert [(row.player_id, row.slot_index) for row in result.selected.locks] == [("b", 1)]
    assert all(
        move.source_slot_index != 0 and move.target_slot_index != 0
        for move in result.selected.moves
    )


def test_a_completed_bench_player_does_not_become_lockable() -> None:
    game = _completed("bench", 100, indices=(0,))
    state = _state((game,), starter_slots=(StarterSlot(0, "UTIL"),))
    assert compare_completed_scores(state, scenario_count=8).selected.locks == ()


def test_equal_values_preserve_verified_future_flexibility_before_fewer_moves() -> None:
    guard = _completed("guard", 30, indices=(0, 1))
    center = _opportunity(
        "center", "future", NOW + timedelta(days=1), ("C",), ((0, 1),), eligible_slot_indices=(1,)
    )
    state = replace(
        _state(
            (guard, center),
            starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "UTIL")),
            observed=(ObservedStarter(1, "guard", ("PG",)),),
        ),
        bench_capacity=1,
    )
    selected = compare_completed_scores(state, scenario_count=8).selected
    assert selected.locks[0].slot_index == 0
    assert len(selected.moves) == 1


def _two_player_options_state():
    slots = tuple(StarterSlot(i, "UTIL") for i in range(3))
    games = tuple(_completed(player, score) for player, score in (("a", 40), ("b", 30)))
    later = _opportunity(
        "later", "later", NOW + timedelta(days=1), ("PG",), ((20, 1),), eligible_slot_indices=(2,)
    )
    return replace(
        _state(
            (*games, later),
            starter_slots=slots,
            observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
        ),
        week_ends_at=NOW + timedelta(days=6),
    )


def test_identical_lineups_share_one_object_across_placements() -> None:
    state = _two_player_options_state()
    options = compare_completed_scores(state, scenario_count=2).alternatives
    assert len(options) > len({option.assignments for option in options})
    assert len({id(option.assignments) for option in options}) == len(
        {option.assignments for option in options}
    )


def test_flexibility_proofs_only_run_for_tied_placements(monkeypatch) -> None:
    calls = []
    prove = completed_scores.future_player_sets

    def counted(*args, **kwargs):
        calls.append(kwargs["excluded"])
        return prove(*args, **kwargs)

    monkeypatch.setattr(completed_scores, "future_player_sets", counted)
    guard = _completed("guard", 30, indices=(0,))
    state = replace(
        _state((guard,), starter_slots=(StarterSlot(0, "PG"),), observed=()),
        observed_starters=(ObservedStarter(0, "guard", ("PG",)),),
        week_ends_at=NOW + timedelta(days=6),
    )
    assert compare_completed_scores(state, scenario_count=2).selected.locks[0].player_id == "guard"
    assert calls == []


def test_only_the_selected_placement_keeps_its_upcoming_decision() -> None:
    state = _two_player_options_state()
    config = WeeklyPlanPolicyConfig(scenario_count=4, postgame_scenario_count=4)
    result = coordinate_completed_continuations(
        state, compare_completed_scores(state, scenario_count=4), config, timedelta(minutes=10)
    )
    assert result.selected.upcoming_decision is not None
    assert result.selected.upcoming_decision.perfect_information_bound is not None
    assert all(
        option.upcoming_decision is None
        for option in result.alternatives
        if option is not result.selected
    )
