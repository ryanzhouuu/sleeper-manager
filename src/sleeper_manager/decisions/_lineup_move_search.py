"""Search executable roster transitions without assuming temporary bench space."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from heapq import heappop, heappush
from itertools import count

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanError
from sleeper_manager.domain.planning import LineupMove

Lineup = tuple[str | None, ...]
MAX_SEARCH_STATES = 10_000


@dataclass(frozen=True)
class LineupMoveSearch:
    """Keep locked/reserve players outside the search and active starters inside it."""

    slot_indices: tuple[int, ...]
    roster: frozenset[str]
    eligible_slots: Mapping[str, frozenset[int]]
    protected: frozenset[str]
    bench_capacity: int | None
    deadline: datetime

    def find(self, observed: Lineup, desired: Lineup) -> tuple[LineupMove, ...]:
        """Find a shortest legal sequence, failing closed at the exploration limit."""
        if observed == desired:
            return ()
        desired_bench_count = len(self.roster) - sum(player is not None for player in desired)
        if self.bench_capacity is not None and desired_bench_count > self.bench_capacity:
            raise WeeklyPlanError("No legal lineup sequence fits the bench capacity")
        serial = count()
        mismatch = _mismatches(observed, desired)
        queue = [((mismatch + 1) // 2, mismatch, next(serial), 0, observed)]
        costs = {observed: 0}
        parents: dict[Lineup, tuple[Lineup, LineupMove]] = {}
        while queue:
            _, _, _, cost, current = heappop(queue)
            if cost != costs[current]:
                continue
            if current == desired:
                return _path(parents, current)
            for target, move in self._neighbors(current):
                next_cost = cost + 1
                if next_cost >= costs.get(target, MAX_SEARCH_STATES + 1):
                    continue
                if target not in costs and len(costs) >= MAX_SEARCH_STATES:
                    raise WeeklyPlanError("No verified legal lineup sequence within search limit")
                costs[target] = next_cost
                parents[target] = current, move
                mismatch = _mismatches(target, desired)
                heappush(
                    queue,
                    (next_cost + (mismatch + 1) // 2, mismatch, next(serial), next_cost, target),
                )
        raise WeeklyPlanError("No legal lineup sequence preserves roster constraints")

    def _neighbors(self, current: Lineup) -> Iterator[tuple[Lineup, LineupMove]]:
        """Yield only exchanges with two legal destinations and moves into vacancies."""
        locations = {player: offset for offset, player in enumerate(current) if player is not None}
        for target, occupant in enumerate(current):
            for player in sorted(self.roster):
                if (
                    player == occupant
                    or self.slot_indices[target] not in self.eligible_slots[player]
                ):
                    continue
                source = locations.get(player)
                if occupant is not None:
                    if source is None and occupant in self.protected:
                        continue
                    if (
                        source is not None
                        and self.slot_indices[source] not in self.eligible_slots[occupant]
                    ):
                        continue
                updated = list(current)
                updated[target] = player
                if source is not None:
                    updated[source] = occupant
                yield (
                    tuple(updated),
                    LineupMove(
                        player,
                        self.slot_indices[source] if source is not None else None,
                        self.slot_indices[target],
                        self.deadline,
                        swap_player_id=occupant,
                    ),
                )
        bench_count = len(self.roster) - len(locations)
        if self.bench_capacity is not None and bench_count < self.bench_capacity:
            for source, benched_player in enumerate(current):
                if benched_player is None or benched_player in self.protected:
                    continue
                updated = list(current)
                updated[source] = None
                yield (
                    tuple(updated),
                    LineupMove(benched_player, self.slot_indices[source], None, self.deadline),
                )


def _mismatches(current: Lineup, desired: Lineup) -> int:
    return sum(left != right for left, right in zip(current, desired, strict=True))


def _path(
    parents: Mapping[Lineup, tuple[Lineup, LineupMove]], target: Lineup
) -> tuple[LineupMove, ...]:
    """Recover the ordered actions only after the complete target has been reached."""
    moves: list[LineupMove] = []
    while target in parents:
        target, move = parents[target]
        moves.append(move)
    return tuple(reversed(moves))
