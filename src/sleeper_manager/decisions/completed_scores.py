"""Joint completed-score acceptance with executable pre-lock lineups and shared continuations."""

from dataclasses import dataclass
from datetime import timedelta
from itertools import product

from sleeper_manager.decisions._lock_flexibility import future_player_sets
from sleeper_manager.decisions._weekly_plan_completion import complete_assignment
from sleeper_manager.decisions._weekly_plan_continuations import PreparedContinuationTopology
from sleeper_manager.decisions._weekly_plan_inputs import (
    fixed_assignments,
    future_opportunities,
    scenario_input,
)
from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.lineup import AssignmentResult, SlotAssignment
from sleeper_manager.decisions.simulation import generate_projection_scenarios, stable_scenario_seed
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import (
    GameOpportunity,
    LineupMove,
    PlannedAssignment,
    TeamWeekState,
)

MAX_LOCK_PLACEMENTS = 4096


@dataclass(frozen=True, slots=True)
class CompletedPlacement:
    """Name the exact irreversible player-game/slot choices and the moves preceding them."""

    locks: tuple[SlotAssignment, ...]
    assignments: tuple[PlannedAssignment, ...]
    moves: tuple[LineupMove, ...]
    expected_score: float = 0.0
    terminal_scores: tuple[float, ...] = ()
    flexibility: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class CompletedScoreComparison:
    """Expose a joint selection, its all-pass baseline, and every legal placement value."""

    selected: CompletedPlacement
    baseline: CompletedPlacement
    alternatives: tuple[CompletedPlacement, ...]


def usable_completed_games(state: TeamWeekState) -> tuple[GameOpportunity, ...]:
    """Exclude expired, passed, benched, and already accepted player-games."""
    passed = {(row.player_id, row.game_id) for row in state.passed_opportunities}
    locked = {row.player_id for row in state.fixed_slots}
    starters = {row.player_id for row in state.observed_starters}
    return tuple(
        sorted(
            (
                game
                for game in state.completed_opportunities
                if game.sleeper_player_id in starters - locked
                and game.rostered_at_tipoff is True
                and game.eligible_slot_indices
                and (game.sleeper_player_id, game.game_id) not in passed
                and not any(
                    later.sleeper_player_id == game.sleeper_player_id
                    and later.game_id != game.game_id
                    and game.scheduled_start < later.scheduled_start <= state.decision_time
                    for later in state.opportunities
                )
            ),
            key=lambda game: (game.sleeper_player_id, game.game_id),
        )
    )


def executable_lock_placements(
    state: TeamWeekState, games: tuple[GameOpportunity, ...]
) -> tuple[CompletedPlacement, ...]:
    """Enumerate complete reachable lock combinations, failing closed at the search budget."""
    slots = tuple(slot for slot in state.starter_slots if slot.index in state.open_slot_indices)
    choices = tuple(
        (
            None,
            *(
                SlotAssignment(
                    slot.index,
                    slot.position,
                    f"completed:{game.sleeper_player_id}:{game.game_id}",
                    game.sleeper_player_id,
                    game.game_id,
                    game.completed_fantasy_score or 0.0,
                )
                for slot in slots
                if slot.index in game.eligible_slot_indices
                and eligible_for_slot(game.eligible_positions, slot.position)
            ),
        )
        for game in games
    )
    results: list[CompletedPlacement] = []
    for count, combination in enumerate(product(*choices)):
        if count >= 100_000 or len(results) >= MAX_LOCK_PLACEMENTS:
            raise WeeklyPlanError("Completed-score placement search limit exceeded")
        locks = tuple(item for item in combination if item is not None)
        if len({item.slot_index for item in locks}) != len(locks) or len(
            {item.player_id for item in locks}
        ) != len(locks):
            continue
        try:
            target = complete_assignment(state, (), AssignmentResult(0, locks))
            view = tuple(
                sorted(
                    (
                        *(
                            PlannedAssignment(row.slot_index, row.slot_position, row.player_id)
                            for row in target
                        ),
                        *(
                            PlannedAssignment(row.slot_index, row.slot_position, row.player_id)
                            for row in state.fixed_slots
                        ),
                    ),
                    key=lambda row: row.slot_index,
                )
            )
            moves = plan_moves(state, view, state.decision_time + timedelta(days=7), timedelta(0))
        except WeeklyPlanError:
            continue
        results.append(CompletedPlacement(locks, view, moves))
    if not results:
        raise WeeklyPlanError("No executable completed-score placement")
    return tuple(results)


def compare_completed_scores(
    state: TeamWeekState,
    *,
    scenario_count: int = 128,
    seed: int = 0,
    tie_tolerance: float = 0.01,
    minimum_confidence: float = 0.0,
) -> CompletedScoreComparison:
    """Choose joint locks from visible scores and independent future draws."""
    if state.is_blocked:
        raise WeeklyPlanError("Cannot compare completed scores from a blocked snapshot")
    placements = executable_lock_placements(state, usable_completed_games(state))
    slots = tuple(slot for slot in state.starter_slots if slot.index in state.open_slot_indices)
    future = future_opportunities(state, state.decision_time)
    inputs = tuple(scenario_input(game) for game in future)
    scenarios = generate_projection_scenarios(
        inputs,
        decision_time=state.decision_time,
        count=scenario_count,
        seed=stable_scenario_seed(
            seed,
            league_id=state.league_id,
            week=state.week,
            roster_id=state.roster_id,
            decision_time=state.decision_time,
        ),
    )
    topology = PreparedContinuationTopology(inputs, slots)
    bits = {slot.index: 1 << i for i, slot in enumerate(slots)}
    keys = tuple(
        (
            frozenset(item.player_id for item in placement.locks if item.player_id is not None),
            topology.full_slot_mask & ~sum(bits[item.slot_index] for item in placement.locks),
        )
        for placement in placements
    )
    requested: dict[frozenset[str], frozenset[int]] = {}
    for excluded, mask in keys:
        requested[excluded] = requested.get(excluded, frozenset()) | {mask}
    fixed_score = sum(item.score for item in fixed_assignments(state.fixed_slots))
    scores: dict[tuple[frozenset[str], int], list[float]] = {key: [] for key in keys}
    for scenario in scenarios:
        tables = topology.best_scores_by_excluded_players(dict(scenario.values), requested)
        for key, values in scores.items():
            values.append(tables[key[0]][key[1]])
    evaluated = []
    for placement, key in zip(placements, keys, strict=True):
        locked_score = fixed_score + sum(item.score for item in placement.locks)
        totals = tuple(locked_score + value for value in scores[key])
        evaluated.append(
            CompletedPlacement(
                placement.locks,
                placement.assignments,
                placement.moves,
                round(sum(totals) / len(totals), 6),
                totals,
                future_player_sets(inputs, slots, excluded=key[0], slot_mask=key[1]),
            )
        )
    baseline = next(item for item in evaluated if not item.locks)
    selected = baseline
    for item in evaluated:
        confidence = sum(
            a > b + tie_tolerance
            for a, b in zip(item.terminal_scores, baseline.terminal_scores, strict=True)
        ) / len(scenarios)
        if item.locks and confidence < minimum_confidence:
            continue
        if minimum_confidence > 0 and any(
            _lock_confidence(item, lock, evaluated, tie_tolerance) < minimum_confidence
            for lock in item.locks
        ):
            continue
        if item.expected_score > selected.expected_score + tie_tolerance:
            selected = item
        elif (
            item.locks
            and selected.locks
            and abs(item.expected_score - selected.expected_score) <= tie_tolerance
        ):
            if item.flexibility > selected.flexibility or (
                not selected.flexibility > item.flexibility
                and (len(item.moves), tuple((row.player_id, row.slot_index) for row in item.locks))
                < (
                    len(selected.moves),
                    tuple((row.player_id, row.slot_index) for row in selected.locks),
                )
            ):
                selected = item
    return CompletedScoreComparison(selected, baseline, tuple(evaluated))


def _lock_confidence(
    choice: CompletedPlacement,
    lock: SlotAssignment,
    alternatives: list[CompletedPlacement],
    tolerance: float,
) -> float:
    """Qualify each joint lock against the best legal continuation without that player-game."""
    other = max(
        (
            row
            for row in alternatives
            if not any(
                (item.player_id, item.game_id) == (lock.player_id, lock.game_id)
                for item in row.locks
            )
        ),
        key=lambda row: row.expected_score,
    )
    return sum(
        a > b + tolerance
        for a, b in zip(choice.terminal_scores, other.terminal_scores, strict=True)
    ) / len(choice.terminal_scores)
