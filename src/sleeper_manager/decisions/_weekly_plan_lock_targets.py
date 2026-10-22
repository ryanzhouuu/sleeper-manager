"""Use live move legality when valuing pregame starter realignment before simulated locks."""

from dataclasses import replace

from sleeper_manager.decisions._weekly_plan_inputs import opportunity_id
from sleeper_manager.decisions.completed_scores import executable_lock_placements
from sleeper_manager.decisions.lineup import AssignmentResult, SlotAssignment
from sleeper_manager.domain.planning import (
    GameOpportunity,
    ObservedStarter,
    PlanningGameStatus,
    TeamWeekState,
)

LockChoices = tuple[tuple[tuple[str, str, int], ...], ...]


def recourse_lock_targets(
    state: TeamWeekState,
    batch: tuple[GameOpportunity, ...],
    completions: dict[AssignmentResult, tuple[SlotAssignment, ...]],
) -> dict[AssignmentResult, LockChoices]:
    """Prepare score-independent executable placements before either scenario sample is revealed."""
    positions = {row.player_id: row.eligible_positions for row in state.observed_starters}
    for game in state.opportunities:
        positions.setdefault(game.sleeper_player_id, game.eligible_positions)
    positions.update({game.sleeper_player_id: game.eligible_positions for game in batch})
    results = {}
    for assignment, target in completions.items():
        players = {row.player_id for row in assignment.assignments}
        games = tuple(
            replace(
                game,
                status=PlanningGameStatus.FINAL,
                completed_fantasy_score=1,
                finalized_at=state.decision_time,
            )
            for game in batch
            if game.sleeper_player_id in players
        )
        keys = {(game.sleeper_player_id, game.game_id): opportunity_id(game) for game in games}
        simulated = replace(
            state,
            observed_starters=tuple(
                ObservedStarter(row.slot_index, row.player_id, positions[row.player_id])
                for row in target
                if row.player_id is not None
            )
            + tuple(
                row
                for row in state.observed_starters
                if row.slot_index not in state.open_slot_indices
            ),
            opportunities=tuple(
                next(
                    (
                        done
                        for done in games
                        if done.sleeper_player_id == game.sleeper_player_id
                        and done.game_id == game.game_id
                    ),
                    replace(game, status=PlanningGameStatus.FINAL, finalized_at=state.decision_time)
                    if game.game_id in {done.game_id for done in games}
                    else game,
                )
                for game in state.opportunities
            ),
        )
        results[assignment] = tuple(
            tuple(
                (
                    keys[(row.player_id or "", row.game_id or "")],
                    row.player_id or "",
                    row.slot_index,
                )
                for row in placement.locks
            )
            for placement in executable_lock_placements(simulated, games)
        )
    return results
