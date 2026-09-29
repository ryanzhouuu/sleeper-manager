"""Contract tests for capture-time companion snapshots."""

from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureError
from sleeper_manager.domain.forecast_context import (
    ForecastCaptureContext,
    ForecastContextEligibility,
    ForecastContextGame,
    ForecastContextGap,
    ForecastContextGapCode,
    ForecastContextRoster,
)
from sleeper_manager.domain.nba import GameStatus

AT = datetime(2026, 9, 25, 18, tzinfo=UTC)


def roster(roster_id: int = 1) -> ForecastContextRoster:
    """Build one roster read at the capture instant."""

    return ForecastContextRoster(
        roster_id=roster_id,
        player_ids=("1000", "1001"),
        starter_ids=("1000", None),
        reserve_ids=(),
        read_at=AT,
    )


def context(**overrides: object) -> ForecastCaptureContext:
    """Build a complete manager-versus-opponent snapshot."""

    values: dict[str, object] = {
        "receipt_id": "receipt-1",
        "league_id": "league-1",
        "season": "2026",
        "week": 1,
        "matchup_id": 7,
        "bye": False,
        "manager": roster(1),
        "opponent": roster(2),
        "eligibility": (
            ForecastContextEligibility("1000", ("PG",), AT),
            ForecastContextEligibility("1001", ("SF",), AT),
        ),
        "games": (
            ForecastContextGame(
                "game-1",
                "CHI",
                "BOS",
                AT + timedelta(hours=2),
                GameStatus.SCHEDULED,
                AT,
            ),
        ),
        "gaps": (),
        "persisted_at": AT,
    }
    values.update(overrides)
    return ForecastCaptureContext(**values)  # type: ignore[arg-type]


def test_context_preserves_roster_order_and_sorts_games() -> None:
    """Keep lineup order while giving games a stable identity order."""

    stored = context(
        games=(
            ForecastContextGame(
                "game-2", "LAL", "GSW", AT + timedelta(days=1), GameStatus.SCHEDULED, AT
            ),
            ForecastContextGame(
                "game-1", "CHI", "BOS", AT + timedelta(hours=2), GameStatus.SCHEDULED, AT
            ),
        )
    )

    assert stored.manager is not None
    assert stored.manager.starter_ids == ("1000", None)
    assert tuple(game.game_id for game in stored.games) == ("game-1", "game-2")


def test_bye_context_records_no_opponent() -> None:
    """A bye is explicit and does not look like a failed opponent read."""

    stored = context(matchup_id=None, bye=True, opponent=None)

    assert stored.bye is True
    assert stored.opponent is None


def test_context_requires_explicit_gaps_for_missing_companion_data() -> None:
    """Refuse a snapshot that omits a roster or matchup without saying why."""

    with pytest.raises(ForecastCaptureError, match="matchup gap"):
        context(matchup_id=None)
    with pytest.raises(ForecastCaptureError, match="manager roster gap"):
        context(manager=None)
    later = ForecastContextRoster(
        roster_id=1,
        player_ids=("1000",),
        starter_ids=("1000",),
        reserve_ids=(),
        read_at=AT + timedelta(seconds=1),
    )
    with pytest.raises(ForecastCaptureError, match="follows its persistence"):
        context(manager=later)


def test_gap_context_can_omit_the_failed_source() -> None:
    """A failed matchup read remains explicit instead of becoming an empty roster."""

    stored = context(
        matchup_id=None,
        manager=None,
        opponent=None,
        eligibility=(),
        games=(),
        gaps=(
            ForecastContextGap(ForecastContextGapCode.MATCHUP, "league-1", "matchup read failed"),
            ForecastContextGap(ForecastContextGapCode.ROSTER, "manager", "roster read failed"),
        ),
    )

    assert stored.manager is None
    assert stored.gaps[0].code is ForecastContextGapCode.MATCHUP
