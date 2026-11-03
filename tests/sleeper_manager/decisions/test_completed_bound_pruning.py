"""Skipping placements by hindsight bound must not change any recommendation."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions import _coordinated_continuations as continuations
from sleeper_manager.decisions.completed_scores import compare_completed_scores
from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import ObservedStarter, StarterSlot
from tests.sleeper_manager.decisions.test_exhaustive_completed_search import _six_player_state
from tests.sleeper_manager.decisions.test_joint_completed_scores import _completed
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

CONFIG = WeeklyPlanPolicyConfig(
    scenario_count=32, postgame_scenario_count=8, minimum_lock_in_confidence=0.7
)


def _stochastic_state(scores=(70, 44, 31, 18)):
    players = tuple(f"p{i}" for i in range(len(scores)))
    slots = tuple(StarterSlot(i, "UTIL") for i in range(4))
    finals = tuple(
        _completed(player, score, indices=tuple(range(4)))
        for player, score in zip(players, scores, strict=True)
    )
    later = tuple(
        _opportunity(
            player,
            f"later-{player}",
            NOW + timedelta(hours=20 + i * 12),
            ("PG",),
            ((90 - i * 10, 0.5), (10 + i, 0.5)),
            eligible_slot_indices=tuple(range(4)),
        )
        for i, player in enumerate((*players, "bench"))
    )
    return replace(
        _state(
            (*finals, *later),
            starter_slots=slots,
            observed=tuple(ObservedStarter(i, player, ("PG",)) for i, player in enumerate(players)),
        ),
        week_ends_at=NOW + timedelta(days=6),
    )


def _full_search(monkeypatch, state, config):
    monkeypatch.setattr(continuations, "_BOUND_MARGIN_TOLERANCES", 1e12)
    plan = build_team_recommendation(state, policy=config)
    monkeypatch.undo()
    return plan


@pytest.mark.parametrize(
    ("state", "config"),
    (
        (_stochastic_state(), CONFIG),
        (_stochastic_state((55, 52, 49, 46)), CONFIG),
        (_stochastic_state((95, 12, 11, 10)), replace(CONFIG, minimum_lock_in_confidence=0.95)),
        (
            _six_player_state(),
            WeeklyPlanPolicyConfig(scenario_count=2, postgame_scenario_count=2),
        ),
    ),
)
def test_skipping_bounded_placements_matches_the_full_search(monkeypatch, state, config) -> None:
    full = _full_search(monkeypatch, state, config)
    pruned = build_team_recommendation(state, policy=config)

    assert pruned.material_hash == full.material_hash
    assert pruned.score_actions == full.score_actions
    assert pruned.moves == full.moves
    assert pruned.desired_assignments == full.desired_assignments
    assert pruned.expected_terminal_score == full.expected_terminal_score
    assert pruned.observed_terminal_score == full.observed_terminal_score


def test_hindsight_totals_bound_every_continuation_on_the_same_draws() -> None:
    state = _stochastic_state()
    bounds = compare_completed_scores(state, scenario_count=CONFIG.scenario_count)
    full = continuations.coordinate_completed_continuations(
        state,
        bounds,
        replace(CONFIG, minimum_lock_in_confidence=0),
        timedelta(minutes=10),
    )
    by_locks = {option.locks: option for option in bounds.alternatives}
    assert full.alternatives
    for option in full.alternatives:
        assert all(
            value <= bound + 1e-5
            for value, bound in zip(
                option.terminal_scores, by_locks[option.locks].terminal_scores, strict=True
            )
        )


def test_clearly_dominated_placements_are_not_fully_scored(monkeypatch) -> None:
    calls = []
    score = continuations._score_continuation

    def counted(*args):
        calls.append(args[0])
        return score(*args)

    monkeypatch.setattr(continuations, "_score_continuation", counted)
    state = _stochastic_state((400, 2, 1, 0))
    bounds = compare_completed_scores(state, scenario_count=CONFIG.scenario_count)
    result = continuations.coordinate_completed_continuations(
        state, bounds, CONFIG, timedelta(minutes=10)
    )
    assert len(result.alternatives) < len(bounds.alternatives)
    assert result.selected.locks[0].player_id == "p0"
