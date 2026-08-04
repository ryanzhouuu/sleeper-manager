from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any


class ScoringCompatibilityError(ValueError):
    """Raised when a league scoring rule cannot be evaluated safely."""


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
        supported_fields = {
            "pts",
            "reb",
            "ast",
            "stl",
            "blk",
            "to",
            "tpm",
            "dd",
            "td",
            "tf",
            "ff",
            "bonus_pt_40p",
            "bonus_pt_50p",
            "bonus_ast_15p",
            "bonus_reb_20p",
        }
        unknown_nonzero: list[str] = []
        for key, value in values.items():
            if key in supported_fields:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as error:
                raise ScoringCompatibilityError(
                    f"Scoring field {key!r} has a non-numeric value"
                ) from error
            if not isfinite(numeric_value):
                raise ScoringCompatibilityError(f"Scoring field {key!r} is not finite")
            if numeric_value != 0:
                unknown_nonzero.append(key)

        if unknown_nonzero:
            fields = ", ".join(sorted(unknown_nonzero))
            raise ScoringCompatibilityError(f"Unsupported nonzero Sleeper scoring fields: {fields}")

        def number(key: str) -> float:
            value = values.get(key, 0)
            try:
                result = float(value)
            except (TypeError, ValueError) as error:
                raise ScoringCompatibilityError(
                    f"Scoring field {key!r} has a non-numeric value"
                ) from error
            if not isfinite(result):
                raise ScoringCompatibilityError(f"Scoring field {key!r} is not finite")
            return result

        return cls(
            points=number("pts"),
            rebounds=number("reb"),
            assists=number("ast"),
            steals=number("stl"),
            blocks=number("blk"),
            turnovers=number("to"),
            three_pointers_made=number("tpm"),
            double_double=number("dd"),
            triple_double=number("td"),
            technical_foul=number("tf"),
            flagrant_foul=number("ff"),
            bonus_40_points=number("bonus_pt_40p"),
            bonus_50_points=number("bonus_pt_50p"),
            bonus_15_assists=number("bonus_ast_15p"),
            bonus_20_rebounds=number("bonus_reb_20p"),
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
