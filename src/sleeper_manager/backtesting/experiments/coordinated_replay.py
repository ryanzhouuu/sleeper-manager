"""Apply coordinated proposals in replay only when the simulated manager performs each action."""

from collections.abc import Mapping
from datetime import datetime

from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.domain.lineup_moves import apply_lineup_moves
from sleeper_manager.domain.lock_in import (
    LockInDecision,
    LockInDecisionKind,
    LockInEvaluation,
    LockInEvaluationKind,
)
from sleeper_manager.domain.planning import WeeklyPlan


def apply_score_actions(
    state: ReplayState,
    plan: WeeklyPlan,
    lineup: Mapping[int, str],
    at: datetime,
) -> tuple[ReplayState, dict[int, str], tuple[LockInEvaluation, ...]]:
    """Apply validated pre-lock moves, exact locks/passes, then the remaining lineup actions."""
    slots = {row.slot_index: lineup.get(row.slot_index) for row in plan.observed_assignments}
    moved = slots
    aligned = False
    evaluations = []
    for action in sorted(plan.score_actions, key=lambda row: row.kind is LockInDecisionKind.LOCK):
        if action.kind is LockInDecisionKind.LOCK and not aligned:
            moved = apply_lineup_moves(plan.moves[: plan.pre_lock_move_count], moved)
            aligned = True
        if action.kind is LockInDecisionKind.LOCK:
            assert action.slot_index is not None
            if moved.get(action.slot_index) != action.player_id:
                raise ValueError(
                    "Coordinated replay lock does not match the actual starter position"
                )
        player_game = next(
            row
            for row in state.player_games
            if (row.sleeper_id, row.game_id) == (action.player_id, action.game_id)
        )
        candidate = state.candidate_at(player_game, at)
        if candidate is None:
            raise ValueError("Coordinated replay action has no currently eligible candidate")
        decision = LockInDecision(
            at,
            action.kind,
            action.player_id,
            action.game_id,
            action.slot_index,
            plan.expected_terminal_score or 0,
            (plan.expected_terminal_score or 0) - (action.alternative_expected_score or 0),
            plan.input_version,
            "Joint executable team recommendation.",
        )
        state = state.apply_decision(candidate, decision)
        evaluations.append(
            LockInEvaluation(
                at,
                LockInEvaluationKind(action.kind.value),
                action.player_id,
                action.game_id,
                action.deadline,
                plan.input_version,
                plan.manager_policy_version,
                ("joint_team_comparison",),
                (("planner_version", plan.planner_version),),
                action.score,
                action.alternative_expected_score,
                confidence=action.confidence or 0,
                decision=decision,
            )
        )
    if not aligned:
        moved = apply_lineup_moves(plan.moves[: plan.pre_lock_move_count], moved)
    moved = apply_lineup_moves(plan.moves[plan.pre_lock_move_count :], moved)
    return (
        state,
        {index: player for index, player in moved.items() if player is not None},
        tuple(evaluations),
    )
