from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LineupAssignment:
    slot: str
    player_id: str
    projected_points: float


def projected_lineup_total(assignments: tuple[LineupAssignment, ...]) -> float:
    return round(sum(assignment.projected_points for assignment in assignments), 2)
