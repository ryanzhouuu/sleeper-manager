"""Use the bounded upcoming-game planner for every joint completed-score alternative."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._recovery_refresh import due_recovery_forfeitures
from sleeper_manager.decisions._weekly_plan_inputs import (
    future_opportunities,
    next_actionable_batch,
    scenario_input,
)
from sleeper_manager.decisions._weekly_plan_models import (
    WeeklyPlanDecision,
    WeeklyPlanError,
    WeeklyPlanPolicyConfig,
)
from sleeper_manager.decisions._weekly_plan_recovery import recovery_bench_capacity
from sleeper_manager.decisions._weekly_plan_samples import PreparedPlanSamples
from sleeper_manager.decisions._weekly_plan_scoring import score_weekly_options
from sleeper_manager.decisions.completed_scores import (
    CompletedScoreComparison,
    select_completed_placement,
    usable_completed_games,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PassedOpportunity,
    TeamWeekState,
)

MAX_COORDINATED_CONTINUATIONS = 256


def coordinate_completed_continuations(
    state: TeamWeekState,
    comparison: CompletedScoreComparison,
    config: WeeklyPlanPolicyConfig,
    lead_time: timedelta,
) -> CompletedScoreComparison:
    """Pair all alternatives using identical draws and the same bounded continuation model."""
    universe = tuple(
        scenario_input(game) for game in future_opportunities(state, state.decision_time)
    )
    if not universe:
        return select_completed_placement(
            comparison.alternatives, config.tie_tolerance, config.minimum_lock_in_confidence
        )
    positions = dict(state.roster_eligibility)
    positions.update({row.player_id: row.eligible_positions for row in state.observed_starters})
    for game in state.opportunities:
        positions.setdefault(game.sleeper_player_id, game.eligible_positions)
    completed = usable_completed_games(state)
    samples = PreparedPlanSamples.prepare(state, config, universe)
    cache: dict[tuple[object, ...], tuple[tuple[float, ...], WeeklyPlanDecision | None]] = {}
    evaluated = []
    for option in comparison.alternatives:
        locked_keys = {(row.player_id, row.game_id) for row in option.locks}
        occupied = {row.slot_index for row in option.locks}
        key = (
            tuple(sorted(locked_keys)),
            tuple(sorted(occupied)),
            tuple(
                (row.slot_index, row.player_id)
                for row in option.assignments
                if row.slot_index not in occupied
            ),
        )
        if key not in cache:
            if len(cache) >= MAX_COORDINATED_CONTINUATIONS:
                raise WeeklyPlanError("Coordinated continuation search limit exceeded")
            fixed = tuple(
                FixedSlot(
                    row.slot_index,
                    row.slot_position,
                    row.player_id or "",
                    row.game_id or "",
                    row.score,
                    state.decision_time,
                    "proposed-only",
                    "proposed-only",
                )
                for row in option.locks
            )
            hypothetical = replace(
                state,
                fixed_slots=(*state.fixed_slots, *fixed),
                observed_starters=tuple(
                    ObservedStarter(row.slot_index, row.player_id, positions[row.player_id])
                    for row in option.assignments
                    if row.player_id is not None
                ),
                passed_opportunities=(
                    *state.passed_opportunities,
                    *(
                        PassedOpportunity(
                            game.sleeper_player_id,
                            game.game_id,
                            state.decision_time,
                            "proposed-only",
                            "proposed-only",
                        )
                        for game in completed
                        if (game.sleeper_player_id, game.game_id) not in locked_keys
                    ),
                ),
                recovery_forfeitures=tuple(
                    key
                    for key in state.recovery_forfeitures
                    if key[0] not in {row.player_id for row in fixed}
                ),
            )
            batch = next_actionable_batch(hypothetical)
            allowed = due_recovery_forfeitures(hypothetical, lead_time)
            hypothetical = replace(
                hypothetical,
                recovery_forfeitures=allowed,
                bench_capacity=recovery_bench_capacity(state, state.recovery_obligations),
            )
            decision = None
            if batch is None:
                scores = (
                    sum(row.accepted_fantasy_score for row in hypothetical.fixed_slots),
                ) * config.scenario_count
            else:
                decision = score_weekly_options(
                    hypothetical,
                    config=config,
                    capture_samples=True,
                    sampling_inputs=universe,
                    _samples=samples,
                )
                scores = decision.selected.scenario_terminal_scores
            cache[key] = scores, decision
        scores, decision = cache[key]
        evaluated.append(
            replace(
                option,
                expected_score=round(sum(scores) / len(scores), 6),
                terminal_scores=scores,
                upcoming_decision=decision,
            )
        )
    return select_completed_placement(
        tuple(evaluated), config.tie_tolerance, config.minimum_lock_in_confidence
    )
