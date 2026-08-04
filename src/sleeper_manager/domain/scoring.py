from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BoxScoreLine:
    points: int = 0
    rebounds: int = 0
    assists: int = 0
    steals: int = 0
    blocks: int = 0
    turnovers: int = 0
    three_pointers_made: int = 0
    technical_fouls: int = 0
    flagrant_fouls: int = 0


@dataclass(frozen=True, slots=True)
class ScoringSettings:
    points: float = 0.0
    rebounds: float = 0.0
    assists: float = 0.0
    steals: float = 0.0
    blocks: float = 0.0
    turnovers: float = 0.0
    three_pointers_made: float = 0.0
    double_double: float = 0.0
    triple_double: float = 0.0
    technical_foul: float = 0.0
    flagrant_foul: float = 0.0
    bonus_40_points: float = 0.0
    bonus_50_points: float = 0.0
    bonus_15_assists: float = 0.0
    bonus_20_rebounds: float = 0.0

    @classmethod
    def from_sleeper(cls, values: Mapping[str, Any]) -> "ScoringSettings":
        return cls(
            points=float(values.get("pts", 0)),
            rebounds=float(values.get("reb", 0)),
            assists=float(values.get("ast", 0)),
            steals=float(values.get("stl", 0)),
            blocks=float(values.get("blk", 0)),
            turnovers=float(values.get("to", 0)),
            three_pointers_made=float(values.get("tpm", 0)),
            double_double=float(values.get("dd", 0)),
            triple_double=float(values.get("td", 0)),
            technical_foul=float(values.get("tf", 0)),
            flagrant_foul=float(values.get("ff", 0)),
            bonus_40_points=float(values.get("bonus_pt_40p", 0)),
            bonus_50_points=float(values.get("bonus_pt_50p", 0)),
            bonus_15_assists=float(values.get("bonus_ast_15p", 0)),
            bonus_20_rebounds=float(values.get("bonus_reb_20p", 0)),
        )


def calculate_fantasy_points(line: BoxScoreLine, settings: ScoringSettings) -> float:
    total = (
        line.points * settings.points
        + line.rebounds * settings.rebounds
        + line.assists * settings.assists
        + line.steals * settings.steals
        + line.blocks * settings.blocks
        + line.turnovers * settings.turnovers
        + line.three_pointers_made * settings.three_pointers_made
        + line.technical_fouls * settings.technical_foul
        + line.flagrant_fouls * settings.flagrant_foul
    )

    category_count = sum(
        value >= 10
        for value in (line.points, line.rebounds, line.assists, line.steals, line.blocks)
    )
    if category_count >= 2:
        total += settings.double_double
    if category_count >= 3:
        total += settings.triple_double
    if line.points >= 40:
        total += settings.bonus_40_points
    if line.points >= 50:
        total += settings.bonus_50_points
    if line.assists >= 15:
        total += settings.bonus_15_assists
    if line.rebounds >= 20:
        total += settings.bonus_20_rebounds

    return round(total, 2)
