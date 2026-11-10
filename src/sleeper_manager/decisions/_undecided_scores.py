"""Describe both sides of a Lock/Pass call that is too close to recommend."""

from datetime import datetime

from sleeper_manager.decisions.completed_scores import CompletedPlacement, placement_value
from sleeper_manager.decisions.win_probability import WinObjective
from sleeper_manager.domain.planning_actions import UndecidedScore


def undecided_score(
    player_id: str,
    game_id: str,
    score: float,
    deadline: datetime,
    passed: CompletedPlacement | None,
    locked: CompletedPlacement | None,
    *,
    pass_confidence: float,
    lock_confidence: float,
    objective: WinObjective | None,
) -> UndecidedScore:
    """``passed`` leaves the final unlocked; ``locked`` is the best placement that locks it."""
    slot = (
        None
        if locked is None
        else next(
            (row for row in locked.locks if (row.player_id, row.game_id) == (player_id, game_id)),
            None,
        )
    )

    def chance(option: CompletedPlacement | None) -> float | None:
        return None if option is None or objective is None else placement_value(option, objective)

    return UndecidedScore(
        player_id,
        game_id,
        pass_confidence,
        lock_confidence,
        score=score,
        deadline=deadline,
        lock_slot_index=None if slot is None else slot.slot_index,
        lock_slot_position=None if slot is None else slot.slot_position,
        pass_expected_score=None if passed is None else passed.expected_score,
        lock_expected_score=None if locked is None else locked.expected_score,
        pass_win_chance=chance(passed),
        lock_win_chance=chance(locked),
    )
