"""Share exact future assignments across Lock/Pass alternatives and scenario confidence."""

from dataclasses import dataclass

from sleeper_manager.decisions._weekly_plan_continuations import PreparedContinuationTopology
from sleeper_manager.decisions.simulation import Scenario, ScenarioInput
from sleeper_manager.domain.planning import StarterSlot


@dataclass(frozen=True, slots=True)
class LockInContinuations:
    """Retain complete terminal scores and rounded policy means for the same draws."""

    pass_value: float
    pass_scores: tuple[float, ...]
    lock_options: tuple[tuple[int, float, tuple[float, ...]], ...]


def lock_in_continuations(
    inputs: tuple[ScenarioInput, ...],
    open_slots: tuple[StarterSlot, ...],
    *,
    scenarios: tuple[Scenario, ...],
    player_id: str,
    completed_score: float,
    legal_slot_indices: tuple[int, ...],
    locked_score: float,
) -> LockInContinuations:
    """Match generic rollout semantics while solving each sampled topology only once."""
    topology = PreparedContinuationTopology(inputs, open_slots)
    full_mask = topology.full_slot_mask
    masks = {
        slot.index: full_mask ^ (1 << position)
        for position, slot in enumerate(open_slots)
        if slot.index in legal_slot_indices
    }
    excluded = frozenset({player_id})
    requested = {frozenset(): frozenset({full_mask}), excluded: frozenset(masks.values())}
    pass_remaining: list[float] = []
    lock_remaining: dict[int, list[float]] = {index: [] for index in masks}
    for scenario in scenarios:
        tables = topology.best_scores_by_excluded_players(dict(scenario.values), requested)
        pass_remaining.append(round(tables[frozenset()][full_mask], 6))
        for index, mask in masks.items():
            lock_remaining[index].append(round(tables[excluded][mask], 6))
    return LockInContinuations(
        pass_value=locked_score + round(sum(pass_remaining) / len(scenarios), 6),
        pass_scores=tuple(locked_score + value for value in pass_remaining),
        lock_options=tuple(
            (
                index,
                locked_score
                + round(sum(completed_score + value for value in values) / len(scenarios), 6),
                tuple(locked_score + completed_score + value for value in values),
            )
            for index, values in lock_remaining.items()
        ),
    )
