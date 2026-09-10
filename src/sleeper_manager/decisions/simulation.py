"""Deterministic projection sampling and legal terminal assignment rollout.

This module operates on provider-neutral scenario inputs. Policy and planner
callers normalize their own domain records before crossing this boundary.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    maximum_weight_assignment,
)
from sleeper_manager.domain.projection import ProjectionSnapshot


class SimulationError(ValueError):
    """Report invalid point-in-time evidence or scenario configuration."""


@dataclass(frozen=True, slots=True)
class ScenarioInput:
    """Describe one projected player-game that may fill eligible starter slots."""

    candidate_id: str
    player_id: str
    game_id: str
    eligible_positions: tuple[str, ...]
    projection: ProjectionSnapshot
    eligible_slot_indices: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        """Reject unstable identities and impossible slot restrictions."""

        if not self.candidate_id.strip() or not self.player_id.strip() or not self.game_id.strip():
            raise SimulationError("Scenario inputs require stable candidate, player, and game IDs")
        if not self.eligible_positions:
            raise SimulationError("Scenario inputs require eligible positions")
        if self.eligible_slot_indices is not None:
            if len(set(self.eligible_slot_indices)) != len(self.eligible_slot_indices):
                raise SimulationError("Scenario input slot indices must be unique")
            if any(index < 0 for index in self.eligible_slot_indices):
                raise SimulationError("Scenario input slot indices must be non-negative")


@dataclass(frozen=True, slots=True)
class Scenario:
    """Store one sampled fantasy score for every candidate in stable order."""

    values: tuple[tuple[str, float], ...]

    def value_for(self, candidate_id: str, default: float = 0.0) -> float:
        """Return the sampled score or a caller-supplied missing-value fallback."""

        return dict(self.values).get(candidate_id, default)


def generate_projection_scenarios(
    inputs: Iterable[ScenarioInput],
    *,
    decision_time: datetime,
    count: int,
    seed: int,
) -> tuple[Scenario, ...]:
    """Sample point-in-time projections deterministically by candidate identity."""

    if decision_time.tzinfo is None:
        raise SimulationError("Scenario decision time must be timezone-aware")
    if count <= 0:
        raise SimulationError("Scenario count must be positive")
    records = tuple(sorted(inputs, key=lambda item: item.candidate_id))
    candidate_ids = tuple(item.candidate_id for item in records)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise SimulationError("Scenario inputs require unique candidate IDs")
    for record in records:
        if record.projection.available_as_of > decision_time:
            raise SimulationError(
                f"Projection for {record.candidate_id} is not available at decision time"
            )
    randomizer = random.Random(seed)
    scenarios: list[Scenario] = []
    for _ in range(count):
        values = tuple(
            (record.candidate_id, _sample_projection(record.projection, randomizer))
            for record in records
        )
        scenarios.append(Scenario(values))
    return tuple(scenarios)


def stable_scenario_seed(
    run_seed: int,
    *,
    league_id: str,
    week: int,
    roster_id: int,
    decision_time: datetime,
) -> int:
    """Derive a stable per-decision random seed from team-week identity."""

    payload = f"{run_seed}|{league_id}|{week}|{roster_id}|{decision_time.isoformat()}"
    digest = hashlib.sha256(payload.encode()).digest()
    return int.from_bytes(digest[:8], "big")


def rollout_scenario_terminal_score(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    remaining_inputs: Iterable[ScenarioInput],
    open_slots: tuple[str, ...],
    scenarios: Sequence[Scenario],
    slot_indices: tuple[int, ...] | None = None,
    excluded_candidate_ids: frozenset[str] = frozenset(),
) -> float:
    """Return mean terminal score across legal assignments for all scenarios."""

    remaining = tuple(remaining_inputs)
    fixed_score = sum(candidate.score for candidate in fixed_assignments)
    results = rollout_scenario_assignments(
        fixed_assignments=fixed_assignments,
        remaining_inputs=remaining,
        open_slots=open_slots,
        scenarios=scenarios,
        slot_indices=slot_indices,
        excluded_candidate_ids=excluded_candidate_ids,
    )
    scores = tuple(fixed_score + result.score for result in results)
    return round(sum(scores) / len(scores), 6)


def rollout_scenario_assignments(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    remaining_inputs: Iterable[ScenarioInput],
    open_slots: tuple[str, ...],
    scenarios: Sequence[Scenario],
    slot_indices: tuple[int, ...] | None = None,
    excluded_candidate_ids: frozenset[str] = frozenset(),
) -> tuple[AssignmentResult, ...]:
    """Optimize every scenario while respecting fixed players and exact slot indices."""

    remaining = tuple(remaining_inputs)
    if not scenarios:
        raise SimulationError("Scenario assignment rollout requires scenarios")
    results: list[AssignmentResult] = []
    fixed_players = {candidate.player_id for candidate in fixed_assignments}
    for scenario in scenarios:
        scenario_values = dict(scenario.values)
        candidates: list[AssignmentCandidate] = []
        for record in remaining:
            if record.candidate_id in excluded_candidate_ids:
                continue
            if record.player_id in fixed_players:
                continue
            candidates.append(
                AssignmentCandidate(
                    candidate_id=record.candidate_id,
                    player_id=record.player_id,
                    score=scenario_values.get(record.candidate_id, 0.0),
                    eligible_positions=record.eligible_positions,
                    game_id=record.game_id,
                    eligible_slot_indices=record.eligible_slot_indices,
                )
            )
        results.append(maximum_weight_assignment(candidates, open_slots, slot_indices=slot_indices))
    return tuple(results)


def _sample_projection(projection: ProjectionSnapshot, randomizer: random.Random) -> float:
    """Sample empirical observations or fall back to the distribution mean."""

    observations = projection.distribution.weighted_observations
    if not observations:
        return projection.distribution.expected_value
    values = tuple(value for value, _ in observations)
    weights = tuple(weight for _, weight in observations)
    return randomizer.choices(values, weights=weights, k=1)[0]


__all__ = (
    "Scenario",
    "ScenarioInput",
    "SimulationError",
    "generate_projection_scenarios",
    "rollout_scenario_assignments",
    "rollout_scenario_terminal_score",
    "stable_scenario_seed",
)
