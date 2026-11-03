"""Use the bounded upcoming-game planner for every joint completed-score alternative."""

from array import array
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._recovery_refresh import due_recovery_forfeitures
from sleeper_manager.decisions._weekly_plan_inputs import (
    fixed_assignments,
    future_opportunities,
    next_actionable_batch,
    scenario_input,
)
from sleeper_manager.decisions._weekly_plan_models import (
    WeeklyPlanDecision,
    WeeklyPlanPolicyConfig,
)
from sleeper_manager.decisions._weekly_plan_recovery import recovery_bench_capacity
from sleeper_manager.decisions._weekly_plan_samples import PreparedPlanSamples
from sleeper_manager.decisions._weekly_plan_scoring import _terminal_value, score_weekly_options
from sleeper_manager.decisions.completed_scores import (
    CompletedPlacement,
    CompletedScoreComparison,
    select_completed_placement,
    usable_completed_games,
)
from sleeper_manager.decisions.simulation import ScenarioInput
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    TeamWeekState,
)

# Generous because tie-breaks chain across options that sit within tie tolerance of each other.
_BOUND_MARGIN_TOLERANCES = 25


def coordinate_completed_continuations(
    state: TeamWeekState,
    comparison: CompletedScoreComparison,
    config: WeeklyPlanPolicyConfig,
    lead_time: timedelta,
) -> CompletedScoreComparison:
    """Score only placements whose hindsight bound can still change what is recommended.

    Every continuation is a feasible lineup within its scenario, so a placement's hindsight total
    on the same outer draws bounds its value. Those draws must come from `comparison`.
    """
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
    cache: dict[tuple[object, ...], array[float]] = {}

    def evaluate(option: CompletedPlacement) -> CompletedPlacement:
        """Replace an option's hindsight bound with its full continuation on the same draws."""
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
            hypothetical = _continuation_state(state, option, completed, positions, lead_time)
            if next_actionable_batch(hypothetical) is None:
                terminal = (
                    sum(row.accepted_fantasy_score for row in hypothetical.fixed_slots),
                ) * config.scenario_count
            else:
                terminal = _score_continuation(
                    hypothetical, config, universe, samples
                ).selected.scenario_terminal_scores
            cache[key] = array("d", terminal)
        scores = cache[key]
        return replace(
            option,
            expected_score=round(sum(scores) / len(scores), 6),
            terminal_scores=scores,
        )

    margin = _BOUND_MARGIN_TOLERANCES * config.tie_tolerance
    options = comparison.alternatives
    evaluated: dict[int, CompletedPlacement] = {}
    bests: dict[tuple[object, ...], float] = {}
    for index in sorted(range(len(options)), key=lambda i: -options[i].expected_score):
        option = options[index]
        groups = _value_groups(option, completed)
        if not option.locks or any(
            option.expected_score + margin >= bests.get(group, float("-inf")) for group in groups
        ):
            evaluated[index] = evaluate(option)
            for group in groups:
                bests[group] = max(bests.get(group, float("-inf")), evaluated[index].expected_score)
    while True:
        result = select_completed_placement(
            tuple(evaluated[index] for index in sorted(evaluated)),
            config.tie_tolerance,
            config.minimum_lock_in_confidence,
        )
        contenders = [
            index
            for index, option in enumerate(options)
            if index not in evaluated
            and option.expected_score + margin >= result.selected.expected_score
        ]
        if not contenders:
            break
        evaluated.update({index: evaluate(options[index]) for index in contenders})
    selected = result.selected
    hypothetical = _continuation_state(state, selected, completed, positions, lead_time)
    batch = next_actionable_batch(hypothetical)
    if batch is None:
        return result
    future = future_opportunities(hypothetical, batch[0].scheduled_start)
    bound = _terminal_value(
        fixed_assignments=fixed_assignments(hypothetical.fixed_slots),
        remaining_inputs=tuple(scenario_input(game) for game in (*batch, *future)),
        open_slots=tuple(
            slot for slot in state.starter_slots if slot.index in hypothetical.open_slot_indices
        ),
        scenarios=samples.outer,
    )
    finalized = replace(
        selected,
        upcoming_decision=replace(
            _score_continuation(hypothetical, config, universe, samples),
            perfect_information_bound=bound,
        ),
    )
    return replace(
        result,
        selected=finalized,
        baseline=finalized if result.baseline is selected else result.baseline,
        alternatives=tuple(finalized if row is selected else row for row in result.alternatives),
    )


def _value_groups(
    option: CompletedPlacement, completed: tuple[GameOpportunity, ...]
) -> tuple[tuple[object, ...], ...]:
    """Name each maximum the recommendation reads: per Pass, per Lock placement, and overall."""
    placed = {(row.player_id, row.game_id): row.slot_index for row in option.locks}
    return (
        ("overall",),
        *(
            ("lock", key, placed[key]) if key in placed else ("pass", key)
            for key in ((game.sleeper_player_id, game.game_id) for game in completed)
        ),
    )


def _score_continuation(
    hypothetical: TeamWeekState,
    config: WeeklyPlanPolicyConfig,
    universe: tuple[ScenarioInput, ...],
    samples: PreparedPlanSamples,
) -> WeeklyPlanDecision:
    """Decisions retain per-sample detail, so only the selected placement's result is kept."""
    return score_weekly_options(
        hypothetical,
        config=config,
        capture_samples=True,
        sampling_inputs=universe,
        _samples=samples,
        _compute_bound=False,
    )


def _continuation_state(
    state: TeamWeekState,
    option: CompletedPlacement,
    completed: tuple[GameOpportunity, ...],
    positions: dict[str, tuple[str, ...]],
    lead_time: timedelta,
) -> TeamWeekState:
    """Hypothesize one full Lock/Pass placement without changing manager observations."""
    locked_keys = {(row.player_id, row.game_id) for row in option.locks}
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
    return replace(
        hypothetical,
        recovery_forfeitures=due_recovery_forfeitures(hypothetical, lead_time),
        bench_capacity=recovery_bench_capacity(state, state.recovery_obligations),
    )
