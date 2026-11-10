"""Joint completed-score acceptance with executable pre-lock lineups and shared continuations."""

from array import array
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache, partial

from sleeper_manager.decisions._lock_flexibility import future_player_sets
from sleeper_manager.decisions._weekly_plan_completion import complete_assignment
from sleeper_manager.decisions._weekly_plan_continuations import PreparedContinuationTopology
from sleeper_manager.decisions._weekly_plan_inputs import (
    fixed_assignments,
    future_opportunities,
    scenario_input,
)
from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanDecision, WeeklyPlanError
from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.lineup import AssignmentResult, SlotAssignment
from sleeper_manager.decisions.simulation import generate_projection_scenarios, stable_scenario_seed
from sleeper_manager.decisions.win_probability import WinObjective
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import (
    GameOpportunity,
    LineupMove,
    PlannedAssignment,
    TeamWeekState,
)


class _ShiftedScores(Sequence[float]):
    """Share one continuation's samples among placements differing only by locked points."""

    __slots__ = ("_offset", "_samples")

    def __init__(self, offset: float, samples: array[float]) -> None:
        self._offset = offset
        self._samples = samples

    def __len__(self) -> int:
        return len(self._samples)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        if isinstance(index, slice):
            return [self._offset + value for value in self._samples[index]]
        return self._offset + self._samples[index]

    def __iter__(self) -> Iterator[float]:
        offset = self._offset
        return (offset + value for value in self._samples)


@dataclass(frozen=True, slots=True)
class CompletedPlacement:
    """Name the exact irreversible player-game/slot choices and the moves preceding them."""

    locks: tuple[SlotAssignment, ...]
    assignments: tuple[PlannedAssignment, ...]
    moves: tuple[LineupMove, ...]
    expected_score: float = 0.0
    terminal_scores: Sequence[float] = ()
    flexibility: frozenset[int] = frozenset()
    upcoming_decision: WeeklyPlanDecision | None = None
    flexibility_proof: Callable[[], frozenset[int]] | None = None
    win_chance: float | None = None

    def proven_flexibility(self) -> frozenset[int]:
        """Defer costly proofs because only tied expected scores ever compare them."""
        return self.flexibility_proof() if self.flexibility_proof else self.flexibility


def placement_value(option: CompletedPlacement, objective: WinObjective | None) -> float:
    """Expected points, or the chance to win when an objective is set."""
    if objective is None:
        return option.expected_score
    if option.win_chance is not None:
        return option.win_chance
    return objective.chance(option.terminal_scores)


@dataclass(frozen=True, slots=True)
class CompletedScoreComparison:
    """Expose a joint selection, its all-pass baseline, and each placement value still needed."""

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
    """Enumerate every reachable lock combination, pruning slot/player collisions early."""
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
    views: dict[tuple[PlannedAssignment, ...], tuple[PlannedAssignment, ...]] = {}
    plans: dict[tuple[PlannedAssignment, ...], tuple[LineupMove, ...] | None] = {}
    for locks in _compatible_locks(choices):
        try:
            target = complete_assignment(state, (), AssignmentResult(0, locks))
        except WeeklyPlanError:
            continue
        built = tuple(
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
        view = views.setdefault(built, built)
        if view not in plans:
            try:
                plans[view] = plan_moves(
                    state, view, state.decision_time + timedelta(days=7), timedelta(0)
                )
            except WeeklyPlanError:
                plans[view] = None
        moves = plans[view]
        if moves is not None:
            results.append(CompletedPlacement(locks, view, moves))
    if not results:
        raise WeeklyPlanError("No executable completed-score placement")
    return tuple(results)


def _compatible_locks(
    choices: tuple[tuple[SlotAssignment | None, ...], ...],
) -> Iterator[tuple[SlotAssignment, ...]]:
    """Preserve Cartesian-product order while abandoning only infeasible partial placements."""

    def visit(
        index: int,
        occupied: frozenset[int],
        players: frozenset[str | None],
        locks: tuple[SlotAssignment, ...],
    ) -> Iterator[tuple[SlotAssignment, ...]]:
        if index == len(choices):
            yield locks
            return
        for choice in choices[index]:
            if choice is None:
                yield from visit(index + 1, occupied, players, locks)
            elif choice.slot_index not in occupied and choice.player_id not in players:
                yield from visit(
                    index + 1,
                    occupied | {choice.slot_index},
                    players | {choice.player_id},
                    (*locks, choice),
                )

    yield from visit(0, frozenset(), frozenset(), ())


def compare_completed_scores(
    state: TeamWeekState,
    *,
    scenario_count: int = 128,
    seed: int = 0,
    tie_tolerance: float = 0.01,
    minimum_confidence: float = 0.0,
    objective: WinObjective | None = None,
) -> CompletedScoreComparison:
    """Choose joint locks from visible scores and independent future draws.

    Totals here are hindsight bounds, so with an objective each win chance is an upper bound.
    """
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
        ),
    )
    topology = PreparedContinuationTopology(inputs, slots)
    bits = {slot.index: 1 << i for i, slot in enumerate(slots)}
    shared_keys: dict[tuple[frozenset[str], int], tuple[frozenset[str], int]] = {}
    keys = tuple(
        shared_keys.setdefault(key, key)
        for key in (
            (
                frozenset(item.player_id for item in placement.locks if item.player_id is not None),
                topology.full_slot_mask & ~sum(bits[item.slot_index] for item in placement.locks),
            )
            for placement in placements
        )
    )
    requested: dict[frozenset[str], frozenset[int]] = {}
    for excluded, mask in keys:
        requested[excluded] = requested.get(excluded, frozenset()) | {mask}
    fixed_score = sum(item.score for item in fixed_assignments(state.fixed_slots))
    scores: dict[tuple[frozenset[str], int], array[float]] = {key: array("d") for key in keys}
    for scenario in scenarios:
        tables = topology.best_scores_by_excluded_players(dict(scenario.values), requested)
        for key, values in scores.items():
            values.append(tables[key[0]][key[1]])
    prove_flexibility = lru_cache(maxsize=16)(
        lambda excluded, mask: future_player_sets(inputs, slots, excluded=excluded, slot_mask=mask)
    )
    proofs = {key: partial(prove_flexibility, *key) for key in scores}
    evaluated = []
    chances: dict[tuple[object, ...], float] = {}
    for placement, key in zip(placements, keys, strict=True):
        locked_score = fixed_score + sum(item.score for item in placement.locks)
        totals = _ShiftedScores(locked_score, scores[key])
        if objective is not None and (key, locked_score) not in chances:
            chances[(key, locked_score)] = objective.chance(totals)
        evaluated.append(
            CompletedPlacement(
                placement.locks,
                placement.assignments,
                placement.moves,
                round(sum(totals) / len(scores[key]), 6),
                totals,
                flexibility_proof=proofs[key],
                win_chance=chances.get((key, locked_score)),
            )
        )
    return select_completed_placement(
        tuple(evaluated), tie_tolerance, minimum_confidence, objective
    )


def select_completed_placement(
    options: tuple[CompletedPlacement, ...],
    tie_tolerance: float,
    minimum_confidence: float,
    objective: WinObjective | None = None,
) -> CompletedScoreComparison:
    """Apply identical confidence, flexibility, and movement preferences to paired continuations.

    An objective first keeps the placements within its tolerance of the best win chance; the
    usual expected-point rules then choose among them. Confidence stays a per-draw point
    comparison, which ranks draws exactly as win chance does.
    """
    evaluated = list(options)
    baseline = next(item for item in evaluated if not item.locks)
    opposing: dict[tuple[str | None, str | None], CompletedPlacement] = {}
    confidences: dict[tuple[int, str | None, str | None], float] = {}

    def lock_confidence(choice: CompletedPlacement, lock: SlotAssignment) -> float:
        """Preserve the first best opposing action and reuse identical paired samples."""
        key = lock.player_id, lock.game_id
        if key not in opposing:
            opposing[key] = max(
                (
                    row
                    for row in evaluated
                    if not any((item.player_id, item.game_id) == key for item in row.locks)
                ),
                key=lambda row: placement_value(row, objective),
            )
        sample_key = id(choice.terminal_scores), *key
        if sample_key not in confidences:
            confidences[sample_key] = _lock_confidence(choice, opposing[key], tie_tolerance)
        return confidences[sample_key]

    def admissible(item: CompletedPlacement) -> bool:
        if minimum_confidence <= 0 or not item.locks:
            return True
        confidence = sum(
            a > b + tie_tolerance
            for a, b in zip(item.terminal_scores, baseline.terminal_scores, strict=True)
        ) / len(item.terminal_scores)
        return confidence >= minimum_confidence and all(
            lock_confidence(item, lock) >= minimum_confidence for lock in item.locks
        )

    candidates = [item for item in evaluated if admissible(item)]
    if objective is not None:
        best = max(placement_value(item, objective) for item in candidates)
        candidates = [
            item
            for item in candidates
            if placement_value(item, objective) >= best - objective.tie_tolerance
        ]
    selected = (
        baseline
        if any(item is baseline for item in candidates)
        else max(candidates, key=lambda item: item.expected_score)
    )
    for item in candidates:
        if item.expected_score > selected.expected_score + tie_tolerance:
            selected = item
        elif (
            item.locks
            and selected.locks
            and abs(item.expected_score - selected.expected_score) <= tie_tolerance
        ):
            flexibility = item.proven_flexibility()
            selected_flexibility = selected.proven_flexibility()
            verified = bool(flexibility and selected_flexibility)
            if (verified and flexibility > selected_flexibility) or (
                not (verified and selected_flexibility > flexibility)
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
    other: CompletedPlacement,
    tolerance: float,
) -> float:
    """Qualify each joint lock against the best legal continuation without that player-game."""
    return sum(
        a > b + tolerance
        for a, b in zip(choice.terminal_scores, other.terminal_scores, strict=True)
    ) / len(choice.terminal_scores)
