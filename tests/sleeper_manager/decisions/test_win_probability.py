from dataclasses import replace

import pytest

from sleeper_manager.decisions.win_probability import (
    Finish,
    WinObjective,
    game_outcomes,
    starter_finish,
    team_finish,
    win_probability,
)
from sleeper_manager.domain.projection import ProjectionDistribution


def test_game_outcomes_take_mid_quantiles_of_kept_observations() -> None:
    distribution = ProjectionDistribution.from_weighted_observations(
        ((10.0, 1.0), (20.0, 1.0), (30.0, 2.0))
    )

    assert game_outcomes(distribution, count=4) == (10.0, 20.0, 30.0, 30.0)


def test_game_outcomes_fit_a_normal_without_observations() -> None:
    distribution = replace(
        ProjectionDistribution.from_weighted_observations(((10.0, 1.0), (30.0, 1.0))),
        weighted_observations=(),
    )

    outcomes = game_outcomes(distribution, count=2)

    assert outcomes[0] == pytest.approx(20 - 6.745, abs=0.01)
    assert outcomes[1] == pytest.approx(20 + 6.745, abs=0.01)


def test_game_outcomes_keep_a_certain_score_as_one_outcome() -> None:
    distribution = replace(
        ProjectionDistribution.from_weighted_observations(((12.0, 1.0),)),
        weighted_observations=(),
    )

    assert game_outcomes(distribution) == (12.0,)


def test_one_upcoming_game_is_the_score_that_counts() -> None:
    finish = starter_finish(((10.0, 20.0),))

    assert finish == Finish(15.0, 25.0)


def test_an_earlier_game_is_locked_only_when_it_beats_waiting() -> None:
    finish = starter_finish(((0.0, 30.0), (10.0, 20.0)))

    assert finish.mean == pytest.approx(22.5)
    assert finish.variance == pytest.approx(68.75)


def test_a_lockable_score_above_waiting_is_locked() -> None:
    assert starter_finish(((10.0, 20.0),), lockable=16.0) == Finish(16.0, 0.0)


def test_a_lockable_score_below_waiting_is_passed() -> None:
    assert starter_finish(((10.0, 20.0),), lockable=14.0) == Finish(15.0, 25.0)


def test_a_played_final_game_is_known() -> None:
    assert starter_finish((), lockable=9.5) == Finish(9.5, 0.0)


def test_a_starter_without_games_scores_nothing() -> None:
    assert starter_finish(()) == Finish(0.0, 0.0)


def test_starter_finish_rejects_a_game_without_outcomes() -> None:
    with pytest.raises(ValueError):
        starter_finish(((),))


def test_team_finish_adds_locked_points_and_starters() -> None:
    finish = team_finish(40.0, (Finish(15.0, 25.0), Finish(10.0, 9.0)))

    assert finish == Finish(65.0, 34.0)


def test_win_probability_against_a_known_score_counts_ties_half() -> None:
    assert win_probability((90.0, 100.0, 110.0, 120.0), Finish(100.0, 0.0)) == 0.625


def test_win_probability_is_even_for_scores_mirrored_around_the_opponent() -> None:
    assert win_probability((80.0, 120.0), Finish(100.0, 100.0)) == 0.5


def test_win_probability_rises_with_a_lead() -> None:
    assert win_probability((130.0,), Finish(100.0, 100.0)) == pytest.approx(0.9987, abs=1e-4)


def test_win_probability_needs_simulated_finishes() -> None:
    with pytest.raises(ValueError):
        win_probability((), Finish(100.0, 0.0))


def test_win_objective_scores_simulated_totals_against_the_opponent() -> None:
    objective = WinObjective(Finish(100.0, 0.0))

    assert objective.chance((90.0, 110.0)) == 0.5


def test_win_objective_rejects_a_tolerance_outside_a_probability() -> None:
    with pytest.raises(ValueError):
        WinObjective(Finish(100.0, 0.0), tie_tolerance=1.0)
