from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.backtesting.replay.models import ReplayPlayerGame
from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    maximum_weight_assignment,
)
from sleeper_manager.domain.projection import ProjectionSnapshot


class SimulationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScenarioInput:
    candidate_id: str
    player_id: str
    game_id: str
    eligible_positions: tuple[str, ...]
    projection: ProjectionSnapshot
    eligible_slot_indices: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
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
    values: tuple[tuple[str, float], ...]

    def value_for(self, candidate_id: str, default: float = 0.0) -> float:
        return dict(self.values).get(candidate_id, default)


def generate_scenarios(
    player_games: Iterable[ReplayPlayerGame],
    *,
    decision_time: datetime,
    count: int,
    seed: int,
) -> tuple[Scenario, ...]:
    records = tuple(player_games)
    inputs = tuple(
        ScenarioInput(
            candidate_id=_candidate_id(record),
            player_id=record.sleeper_id,
            game_id=record.game_id,
            eligible_positions=record.eligible_positions,
            projection=record.projection
            if record.projection is not None
            else _missing_projection(record),
        )
        for record in records
    )
    return generate_projection_scenarios(
        inputs, decision_time=decision_time, count=count, seed=seed
    )


def generate_projection_scenarios(
    inputs: Iterable[ScenarioInput],
    *,
    decision_time: datetime,
    count: int,
    seed: int,
) -> tuple[Scenario, ...]:
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
    payload = f"{run_seed}|{league_id}|{week}|{roster_id}|{decision_time.isoformat()}"
    digest = hashlib.sha256(payload.encode()).digest()
    return int.from_bytes(digest[:8], "big")


def rollout_terminal_score(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    remaining_games: Iterable[ReplayPlayerGame],
    open_slots: tuple[str, ...],
    scenarios: Sequence[Scenario],
) -> float:
    remaining = tuple(remaining_games)
    inputs = tuple(
        ScenarioInput(
            candidate_id=_candidate_id(record),
            player_id=record.sleeper_id,
            game_id=record.game_id,
            eligible_positions=record.eligible_positions,
            projection=record.projection
            if record.projection is not None
            else _missing_projection(record),
        )
        for record in remaining
    )
    return rollout_scenario_terminal_score(
        fixed_assignments=fixed_assignments,
        remaining_inputs=inputs,
        open_slots=open_slots,
        scenarios=scenarios,
    )


def rollout_scenario_terminal_score(
    *,
    fixed_assignments: tuple[AssignmentCandidate, ...],
    remaining_inputs: Iterable[ScenarioInput],
    open_slots: tuple[str, ...],
    scenarios: Sequence[Scenario],
    slot_indices: tuple[int, ...] | None = None,
    excluded_candidate_ids: frozenset[str] = frozenset(),
) -> float:
    remaining = tuple(remaining_inputs)
    if not scenarios:
        raise SimulationError("Terminal-score rollout requires scenarios")
    scores: list[float] = []
    fixed_players = {candidate.player_id for candidate in fixed_assignments}
    fixed_score = sum(candidate.score for candidate in fixed_assignments)
    for scenario in scenarios:
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
                    score=scenario.value_for(record.candidate_id),
                    eligible_positions=record.eligible_positions,
                    game_id=record.game_id,
                    eligible_slot_indices=record.eligible_slot_indices,
                )
            )
        result = maximum_weight_assignment(candidates, open_slots, slot_indices=slot_indices)
        scores.append(fixed_score + result.score)
    return round(sum(scores) / len(scores), 6)


def pregame_assignment(
    player_games: Iterable[ReplayPlayerGame],
    *,
    decision_time: datetime,
    starter_slots: tuple[str, ...],
) -> AssignmentResult:
    records = tuple(player_games)
    _validate_projection_times(records, decision_time)
    candidates = tuple(
        AssignmentCandidate(
            candidate_id=_candidate_id(record),
            player_id=record.sleeper_id,
            score=record.projection.distribution.expected_value if record.projection else 0.0,
            eligible_positions=record.eligible_positions,
            game_id=record.game_id,
        )
        for record in records
        if record.rostered_at_tipoff
    )
    return maximum_weight_assignment(candidates, starter_slots)


def _sample_distribution(record: ReplayPlayerGame, randomizer: random.Random) -> float:
    assert record.projection is not None
    return _sample_projection(record.projection, randomizer)


def _sample_projection(projection: ProjectionSnapshot, randomizer: random.Random) -> float:
    observations = projection.distribution.weighted_observations
    if not observations:
        return projection.distribution.expected_value
    values = tuple(value for value, _ in observations)
    weights = tuple(weight for _, weight in observations)
    return randomizer.choices(values, weights=weights, k=1)[0]


def _candidate_id(record: ReplayPlayerGame) -> str:
    return (
        f"{record.sleeper_id}:{record.game_id}:"
        f"{record.membership_segment or record.fantasy_team_id}"
    )


def _missing_projection(record: ReplayPlayerGame) -> ProjectionSnapshot:
    raise SimulationError(f"Missing projection for {record.sleeper_id}:{record.game_id}")


def _validate_projection_times(
    records: Sequence[ReplayPlayerGame], decision_time: datetime
) -> None:
    if decision_time.tzinfo is None:
        raise SimulationError("Decision time must be timezone-aware")
    for record in records:
        if record.projection is None:
            raise SimulationError(f"Missing projection for {record.sleeper_id}:{record.game_id}")
        if record.projection.available_as_of > decision_time:
            raise SimulationError(
                f"Projection for {record.sleeper_id}:{record.game_id} is not point-in-time valid"
            )


__all__ = (
    "Scenario",
    "ScenarioInput",
    "SimulationError",
    "generate_scenarios",
    "generate_projection_scenarios",
    "pregame_assignment",
    "rollout_terminal_score",
    "rollout_scenario_terminal_score",
    "stable_scenario_seed",
)
