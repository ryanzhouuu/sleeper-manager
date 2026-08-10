import hashlib
import json
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


_SLEEPER_FIELDS: tuple[tuple[str, str], ...] = (
    ("pts", "points"),
    ("reb", "rebounds"),
    ("ast", "assists"),
    ("stl", "steals"),
    ("blk", "blocks"),
    ("to", "turnovers"),
    ("tpm", "three_pointers_made"),
    ("dd", "double_double"),
    ("td", "triple_double"),
    ("tf", "technical_foul"),
    ("ff", "flagrant_foul"),
    ("bonus_pt_40p", "bonus_40_points"),
    ("bonus_pt_50p", "bonus_50_points"),
    ("bonus_ast_15p", "bonus_15_assists"),
    ("bonus_reb_20p", "bonus_20_rebounds"),
)
_SUPPORTED_FIELDS = frozenset(key for key, _ in _SLEEPER_FIELDS)


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
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

    def __post_init__(self) -> None:
        for key, attribute in _SLEEPER_FIELDS:
            normalized = _finite_number(getattr(self, attribute), key)
            object.__setattr__(self, attribute, normalized)

    @classmethod
    def from_sleeper(cls, values: Mapping[str, Any]) -> "ScoringPolicy":
        unknown_nonzero: list[str] = []
        for key, value in values.items():
            if key in _SUPPORTED_FIELDS:
                continue
            numeric_value = _finite_number(value, key)
            if numeric_value != 0:
                unknown_nonzero.append(key)

        if unknown_nonzero:
            fields = ", ".join(sorted(unknown_nonzero))
            raise ScoringCompatibilityError(f"Unsupported nonzero Sleeper scoring fields: {fields}")

        parsed = {
            attribute: _finite_number(values.get(key, 0), key) for key, attribute in _SLEEPER_FIELDS
        }
        return cls(**parsed)

    @property
    def fingerprint(self) -> str:
        values = {key: getattr(self, attribute) for key, attribute in _SLEEPER_FIELDS}
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def version(self) -> str:
        return f"scoring-policy-v1-{self.fingerprint[:12]}"


@dataclass(frozen=True, slots=True)
class ScoreContribution:
    field: str
    kind: str
    source_value: float
    multiplier: float
    contribution: float


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    total: float
    contributions: tuple[ScoreContribution, ...]

    @property
    def consumed_fields(self) -> tuple[str, ...]:
        return tuple(contribution.field for contribution in self.contributions)


@dataclass(frozen=True, slots=True)
class ScoreParityCase:
    player_id: str
    game_id: str
    box_score: BoxScoreLine
    sleeper_fantasy_points: float


@dataclass(frozen=True, slots=True)
class ScoreParityResult:
    player_id: str
    game_id: str
    calculated_fantasy_points: float
    sleeper_fantasy_points: float
    difference: float
    tolerance: float

    @property
    def matches(self) -> bool:
        return abs(self.difference) <= self.tolerance


def _finite_number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ScoringCompatibilityError(
            f"Scoring field {field!r} has a non-numeric value"
        ) from error
    if not isfinite(result):
        raise ScoringCompatibilityError(f"Scoring field {field!r} is not finite")
    return result


def _add_contribution(
    contributions: list[ScoreContribution],
    *,
    field: str,
    kind: str,
    source_value: float,
    multiplier: float,
) -> float:
    contribution = source_value * multiplier
    if contribution:
        contributions.append(
            ScoreContribution(
                field=field,
                kind=kind,
                source_value=source_value,
                multiplier=multiplier,
                contribution=round(contribution, 2),
            )
        )
    return contribution


def _add_triggered_bonus(
    contributions: list[ScoreContribution],
    *,
    field: str,
    source_value: float,
    bonus: float,
) -> float:
    if bonus:
        contributions.append(
            ScoreContribution(
                field=field,
                kind="bonus",
                source_value=source_value,
                multiplier=bonus,
                contribution=round(bonus, 2),
            )
        )
    return bonus


def calculate_score_breakdown(line: BoxScoreLine, policy: ScoringPolicy) -> ScoreBreakdown:
    contributions: list[ScoreContribution] = []
    total = 0.0

    base_components = (
        ("pts", line.points, policy.points),
        ("reb", line.rebounds, policy.rebounds),
        ("ast", line.assists, policy.assists),
        ("stl", line.steals, policy.steals),
        ("blk", line.blocks, policy.blocks),
        ("to", line.turnovers, policy.turnovers),
        ("tpm", line.three_pointers_made, policy.three_pointers_made),
        ("tf", line.technical_fouls, policy.technical_foul),
        ("ff", line.flagrant_fouls, policy.flagrant_foul),
    )
    for field, source_value, multiplier in base_components:
        total += _add_contribution(
            contributions,
            field=field,
            kind="stat",
            source_value=float(source_value),
            multiplier=multiplier,
        )

    category_count = sum(
        value >= 10
        for value in (line.points, line.rebounds, line.assists, line.steals, line.blocks)
    )
    if category_count >= 2:
        total += _add_triggered_bonus(
            contributions,
            field="dd",
            source_value=float(category_count),
            bonus=policy.double_double,
        )
    if category_count >= 3:
        total += _add_triggered_bonus(
            contributions,
            field="td",
            source_value=float(category_count),
            bonus=policy.triple_double,
        )

    thresholds = (
        ("bonus_pt_40p", line.points >= 40, line.points, policy.bonus_40_points),
        ("bonus_pt_50p", line.points >= 50, line.points, policy.bonus_50_points),
        ("bonus_ast_15p", line.assists >= 15, line.assists, policy.bonus_15_assists),
        ("bonus_reb_20p", line.rebounds >= 20, line.rebounds, policy.bonus_20_rebounds),
    )
    for field, triggered, source_value, multiplier in thresholds:
        if triggered:
            total += _add_triggered_bonus(
                contributions,
                field=field,
                source_value=float(source_value),
                bonus=multiplier,
            )

    return ScoreBreakdown(total=round(total, 2), contributions=tuple(contributions))


def calculate_fantasy_points(line: BoxScoreLine, policy: ScoringPolicy) -> float:
    return calculate_score_breakdown(line, policy).total


def compare_score_parity(
    case: ScoreParityCase,
    policy: ScoringPolicy,
    *,
    tolerance: float = 0.01,
) -> ScoreParityResult:
    if not isfinite(tolerance) or tolerance < 0:
        raise ValueError("Score parity tolerance must be finite and non-negative")
    sleeper_points = _finite_number(case.sleeper_fantasy_points, "sleeper_fantasy_points")
    calculated_points = calculate_fantasy_points(case.box_score, policy)
    rounded_sleeper_points = round(sleeper_points, 2)
    difference = round(calculated_points - rounded_sleeper_points, 2)
    return ScoreParityResult(
        player_id=case.player_id,
        game_id=case.game_id,
        calculated_fantasy_points=calculated_points,
        sleeper_fantasy_points=rounded_sleeper_points,
        difference=difference,
        tolerance=tolerance,
    )


ScoringSettings = ScoringPolicy


__all__ = (
    "BoxScoreLine",
    "ScoreBreakdown",
    "ScoreContribution",
    "ScoreParityCase",
    "ScoreParityResult",
    "ScoringCompatibilityError",
    "ScoringPolicy",
    "ScoringSettings",
    "calculate_fantasy_points",
    "calculate_score_breakdown",
    "compare_score_parity",
)
