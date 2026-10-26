"""Normal multi-player finals retain every legal placement and confidence counterfactual."""

from dataclasses import replace
from datetime import timedelta
from itertools import product
from math import comb, factorial

from sleeper_manager.decisions.completed_scores import (
    _compatible_locks,
    executable_lock_placements,
    usable_completed_games,
)
from sleeper_manager.decisions.lineup import SlotAssignment
from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import FixedSlot, ObservedStarter, PlanStatus, StarterSlot
from tests.sleeper_manager.decisions.test_joint_completed_scores import _completed
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state


def _six_player_state():
    slots = tuple(
        StarterSlot(i, p)
        for i, p in enumerate(("PG", "SG", "G", "SF", "PF", "F", "C", "UTIL", "UTIL"))
    )
    positions = (
        ("PG", "SG"),
        ("PF", "SF"),
        ("PF", "SF", "SG"),
        ("PG", "SG"),
        ("PG", "SF", "SG"),
        ("PG", "SG"),
    )
    slot_indices = (0, 5, 4, 2, 3, 7)
    games = tuple(_completed(f"p{i}", 30 + i, positions[i], tuple(range(9))) for i in range(6))
    fixed_game = _completed("confirmed", 10, ("PG", "SG"), tuple(range(9)))
    future_players = tuple((f"p{i}", positions[i]) for i in range(6)) + (
        ("center", ("C",)),
        ("held", ("PG",)),
    )
    future = tuple(
        _opportunity(
            player,
            f"later-{player}",
            NOW + timedelta(days=1, hours=i),
            pos,
            ((100, 1),),
            eligible_slot_indices=tuple(range(9)),
        )
        for i, (player, pos) in enumerate(future_players)
    )
    fixed = FixedSlot(1, "SG", "confirmed", fixed_game.game_id, 10, NOW, "confirmed", "fixture")
    observed = tuple(
        ObservedStarter(index, f"p{i}", positions[i]) for i, index in enumerate(slot_indices)
    )
    return replace(
        _state(
            (*games, fixed_game, *future),
            starter_slots=slots,
            observed=(
                *observed,
                ObservedStarter(1, "confirmed", ("PG", "SG")),
                ObservedStarter(6, "center", ("C",)),
                ObservedStarter(8, "held", ("PG",)),
            ),
            fixed=(fixed,),
        ),
        bench_capacity=0,
        week_ends_at=NOW + timedelta(days=6),
    )


def test_six_final_scores_complete_beyond_the_old_continuation_cap() -> None:
    state = _six_player_state()
    plan = build_team_recommendation(
        state, policy=WeeklyPlanPolicyConfig(scenario_count=2, postgame_scenario_count=2)
    )

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.blocking_reasons == ()
    assert plan.expected_terminal_score == 810
    assert plan.fixed_slots == state.fixed_slots
    assert {row.player_id for row in plan.score_actions} == {f"p{i}" for i in range(6)}
    assert all(
        row.kind is LockInDecisionKind.PASS and row.confidence == 1 for row in plan.score_actions
    )


def test_exhaustive_placements_survive_both_old_enumeration_limits() -> None:
    slots = tuple(StarterSlot(i, "UTIL") for i in range(6))
    games = tuple(_completed(f"p{i}", 30 + i, indices=tuple(range(6))) for i in range(6))
    state = replace(
        _state(
            games,
            starter_slots=slots,
            observed=tuple(ObservedStarter(i, f"p{i}", ("PG",)) for i in range(6)),
        ),
        bench_capacity=0,
        week_ends_at=NOW + timedelta(days=6),
    )
    options = executable_lock_placements(state, usable_completed_games(state))
    expected = sum(comb(6, k) * factorial(6) // factorial(6 - k) for k in range(7))

    assert expected == len(options) == 13327
    assert (
        len({tuple((row.player_id, row.slot_index) for row in option.locks) for option in options})
        == expected
    )
    assert options[0].locks == ()
    assert all(
        len({row.slot_index for row in option.locks}) == len(option.locks) for option in options
    )


def test_early_pruning_preserves_exhaustive_order_and_same_player_exclusion() -> None:
    choices = tuple(
        (None, *(SlotAssignment(i, "UTIL", f"{player}-{i}", player, player, 1) for i in range(3)))
        for player in ("a", "b", "a")
    )
    expected = []
    for combination in product(*choices):
        locks = tuple(row for row in combination if row is not None)
        if (
            len({row.slot_index for row in locks})
            == len(locks)
            == len({row.player_id for row in locks})
        ):
            expected.append(locks)

    assert tuple(_compatible_locks(choices)) == tuple(expected)
