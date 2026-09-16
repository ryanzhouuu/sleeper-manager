"""Evaluate exact weekly-plan continuations with shared slot-mask tables.

The public planner retains concrete assignments for policy ties and move
generation. This module compresses only their terminal-score calculation and
keeps the generic assignment solver as an independent reference oracle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    candidate_eligible_for_slot,
)
from sleeper_manager.decisions.simulation import ScenarioInput, SimulationError
from sleeper_manager.domain.planning import StarterSlot

_TIE_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class _CandidateEdge:
    """Retain one future candidate's stable identity and eligible slot position."""

    position: int
    candidate_id: str
    score: float = 0.0
    slot_bit: int = 0
    lexicographic_code: int = 0


@dataclass(frozen=True, slots=True)
class _PositionCandidates:
    """Group one player's mutually exclusive candidates for a starter slot."""

    position: int
    candidate_ids: tuple[str, ...]


class PreparedContinuationTopology:
    """Reuse fixed future eligibility topology across sampled score scenarios."""

    def __init__(
        self,
        inputs: Sequence[ScenarioInput],
        open_slots: tuple[StarterSlot, ...],
    ) -> None:
        """Prepare stable player edges and deterministic tie codes once per decision."""

        records = tuple(inputs)
        candidate_ids = tuple(record.candidate_id for record in records)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise SimulationError("Continuation inputs require unique candidate IDs")
        slot_indices = tuple(slot.index for slot in open_slots)
        if len(set(slot_indices)) != len(slot_indices):
            raise SimulationError("Continuation slots require unique indices")

        candidates = tuple(
            AssignmentCandidate(
                candidate_id=record.candidate_id,
                player_id=record.player_id,
                score=0.0,
                eligible_positions=record.eligible_positions,
                game_id=record.game_id,
                eligible_slot_indices=record.eligible_slot_indices,
            )
            for record in records
        )
        edges_by_player: dict[str, list[_CandidateEdge]] = {}
        for position, slot in enumerate(open_slots):
            for candidate in sorted(candidates, key=lambda item: item.candidate_id):
                if candidate_eligible_for_slot(
                    candidate,
                    slot_index=slot.index,
                    slot_position=slot.position,
                ):
                    edges_by_player.setdefault(candidate.player_id, []).append(
                        _CandidateEdge(position, candidate.candidate_id)
                    )

        players: list[tuple[str, tuple[_PositionCandidates, ...]]] = []
        for player_id, edges in sorted(edges_by_player.items()):
            candidate_ids_by_position: dict[int, list[str]] = {}
            for edge in sorted(edges, key=lambda item: (item.position, item.candidate_id)):
                candidate_ids_by_position.setdefault(edge.position, []).append(edge.candidate_id)
            players.append(
                (
                    player_id,
                    tuple(
                        _PositionCandidates(position, tuple(candidate_ids))
                        for position, candidate_ids in sorted(candidate_ids_by_position.items())
                    ),
                )
            )
        self._players = tuple(players)
        self._player_ids = frozenset(player_id for player_id, _ in self._players)
        sorted_candidate_ids = tuple(sorted(candidate_ids))
        self._candidate_ranks = {
            candidate_id: index + 1 for index, candidate_id in enumerate(sorted_candidate_ids)
        }
        lexicographic_base = len(sorted_candidate_ids) + 1
        self._lexicographic_powers = tuple(
            lexicographic_base ** (len(open_slots) - position - 1)
            for position in range(len(open_slots))
        )
        self._state_count = 1 << len(open_slots)
        self._submasks = tuple(
            tuple(mask for mask in range(self._state_count) if mask & ~allowed_mask == 0)
            for allowed_mask in range(self._state_count)
        )

    @property
    def full_slot_mask(self) -> int:
        """Return the mask containing every prepared starter slot."""

        return self._state_count - 1

    @property
    def player_ids(self) -> frozenset[str]:
        """Return players whose exclusion can change a continuation score."""

        return self._player_ids

    def best_scores_by_allowed_mask(
        self,
        scenario_values: Mapping[str, float],
        *,
        excluded_player_ids: frozenset[str] = frozenset(),
        allowed_masks: frozenset[int] | None = None,
    ) -> tuple[float, ...]:
        """Return exact scores, optionally limiting populated allowed slot masks."""

        requested_masks = (
            frozenset(range(self._state_count)) if allowed_masks is None else allowed_masks
        )
        return self.best_scores_by_excluded_players(
            scenario_values,
            {excluded_player_ids: requested_masks},
        )[excluded_player_ids]

    def best_scores_by_excluded_players(
        self,
        scenario_values: Mapping[str, float],
        allowed_masks_by_exclusion: Mapping[frozenset[str], frozenset[int]],
    ) -> dict[frozenset[str], tuple[float, ...]]:
        """Share invariant-player assignment work across exclusion groups."""

        if not allowed_masks_by_exclusion:
            return {}
        requested_masks = frozenset().union(*allowed_masks_by_exclusion.values())
        if any(mask < 0 or mask >= self._state_count for mask in requested_masks):
            raise SimulationError("Allowed continuation slot mask is out of range")
        active_slot_mask = 0
        for mask in requested_masks:
            active_slot_mask |= mask
        scenario_players = self._scenario_players(scenario_values, active_slot_mask)
        exclusion_groups = tuple(allowed_masks_by_exclusion)
        always_excluded = frozenset.intersection(*exclusion_groups)
        varying_players = frozenset.union(*exclusion_groups) - always_excluded
        invariant_players = tuple(
            player
            for player in scenario_players
            if player[0] not in always_excluded and player[0] not in varying_players
        )
        base_scores, base_codes = self._apply_players(
            invariant_players,
            active_slot_mask=active_slot_mask,
        )
        branching_players = tuple(
            player for player in scenario_players if player[0] in varying_players
        )
        branch_bits = {
            player_id: 1 << index for index, (player_id, _) in enumerate(branching_players)
        }
        branch_states = {0: (base_scores, base_codes)}

        def states_for(player_mask: int) -> tuple[list[float], list[int]]:
            """Build one relevant-player subset from its cached ordered parent."""

            if player_mask not in branch_states:
                player_bit = 1 << (player_mask.bit_length() - 1)
                parent_scores, parent_codes = states_for(player_mask ^ player_bit)
                branch_states[player_mask] = self._apply_players(
                    (branching_players[player_bit.bit_length() - 1],),
                    active_slot_mask=active_slot_mask,
                    initial_scores=parent_scores,
                    initial_codes=parent_codes,
                )
            return branch_states[player_mask]

        results: dict[frozenset[str], tuple[float, ...]] = {}
        for excluded_players, masks in allowed_masks_by_exclusion.items():
            allowed_player_mask = sum(
                bit for player_id, bit in branch_bits.items() if player_id not in excluded_players
            )
            scores, codes = states_for(allowed_player_mask)
            results[excluded_players] = self._select_allowed_scores(scores, codes, masks)
        return results

    def _scenario_players(
        self,
        scenario_values: Mapping[str, float],
        active_slot_mask: int,
    ) -> tuple[tuple[str, tuple[_CandidateEdge, ...]], ...]:
        """Choose each player-slot transition's exact scenario winner once."""

        players: list[tuple[str, tuple[_CandidateEdge, ...]]] = []
        for player_id, position_candidates in self._players:
            active_edges: list[_CandidateEdge] = []
            for group in position_candidates:
                if not active_slot_mask & (1 << group.position):
                    continue
                best_candidate_id: str | None = None
                best_candidate_score = float("-inf")
                for candidate_id in group.candidate_ids:
                    candidate_score = scenario_values.get(candidate_id, 0.0)
                    if candidate_score > best_candidate_score + _TIE_TOLERANCE or (
                        abs(candidate_score - best_candidate_score) <= _TIE_TOLERANCE
                        and (
                            best_candidate_id is None
                            or self._candidate_ranks[candidate_id]
                            < self._candidate_ranks[best_candidate_id]
                        )
                    ):
                        best_candidate_id = candidate_id
                        best_candidate_score = candidate_score
                if best_candidate_id is not None:
                    active_edges.append(
                        _CandidateEdge(
                            group.position,
                            best_candidate_id,
                            best_candidate_score,
                            1 << group.position,
                            self._candidate_ranks[best_candidate_id]
                            * self._lexicographic_powers[group.position],
                        )
                    )
            if active_edges:
                players.append((player_id, tuple(active_edges)))
        return tuple(players)

    def _apply_players(
        self,
        players: Sequence[tuple[str, tuple[_CandidateEdge, ...]]],
        *,
        active_slot_mask: int,
        initial_scores: list[float] | None = None,
        initial_codes: list[int] | None = None,
    ) -> tuple[list[float], list[int]]:
        """Apply each player once; descending masks prevent same-player reuse."""

        scores = (
            [float("-inf")] * self._state_count if initial_scores is None else initial_scores.copy()
        )
        lexicographic_codes = (
            [0] * self._state_count if initial_codes is None else initial_codes.copy()
        )
        if initial_scores is None:
            scores[0] = 0.0
        reachable_masks = self._submasks[active_slot_mask]
        for _, active_edges in players:
            for mask in reversed(reachable_masks):
                base_score = scores[mask]
                if base_score == float("-inf"):
                    continue
                for edge in active_edges:
                    if mask & edge.slot_bit:
                        continue
                    candidate_mask = mask | edge.slot_bit
                    candidate_score = base_score + edge.score
                    candidate_lexicographic_code = (
                        lexicographic_codes[mask] + edge.lexicographic_code
                    )
                    incumbent_score = scores[candidate_mask]
                    if candidate_score > incumbent_score + _TIE_TOLERANCE or (
                        candidate_lexicographic_code < lexicographic_codes[candidate_mask]
                        and abs(candidate_score - incumbent_score) <= _TIE_TOLERANCE
                    ):
                        scores[candidate_mask] = candidate_score
                        lexicographic_codes[candidate_mask] = candidate_lexicographic_code
        return scores, lexicographic_codes

    def _select_allowed_scores(
        self,
        scores: Sequence[float],
        lexicographic_codes: Sequence[int],
        requested_masks: frozenset[int],
    ) -> tuple[float, ...]:
        """Select the reference winner for each requested allowed-slot mask."""

        best_scores = [float("-inf")] * self._state_count
        for allowed_mask in sorted(requested_masks):
            best_mask: int | None = None
            best_score = float("-inf")
            best_lexicographic_code = 0
            for mask in self._submasks[allowed_mask]:
                score = scores[mask]
                lexicographic_code = lexicographic_codes[mask]
                if score > best_score + _TIE_TOLERANCE or (
                    abs(score - best_score) <= _TIE_TOLERANCE
                    and (best_mask is None or lexicographic_code < best_lexicographic_code)
                ):
                    best_mask = mask
                    best_score = score
                    best_lexicographic_code = lexicographic_code
            if best_mask is None:
                raise SimulationError(
                    f"Continuation has no feasible assignment for slot mask {allowed_mask}"
                )
            best_scores[allowed_mask] = round(best_score, 6)
        return tuple(best_scores)
