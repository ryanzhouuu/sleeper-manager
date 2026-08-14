from datetime import UTC, datetime

from sleeper_manager.domain.nba import GameStatus, ScheduledGame, SourceMetadata
from sleeper_manager.integrations.nba.travel import travel_context

NOW = datetime(2026, 8, 13, tzinfo=UTC)
SOURCE = SourceMetadata("fixture", "fixture", NOW)


def scheduled_game(
    game_id: str,
    start: datetime,
    venue_id: str,
    city: str,
    state: str,
) -> ScheduledGame:
    return ScheduledGame(
        game_id,
        start,
        GameStatus.FINAL,
        "home",
        "away",
        None,
        SOURCE,
        venue_id=venue_id,
        venue_city=city,
        venue_state=state,
    )


def test_travel_context_uses_event_time_for_timezone_change() -> None:
    previous = scheduled_game(
        "previous", datetime(2025, 3, 7, 1, tzinfo=UTC), "1949", "Phoenix", "AZ"
    )
    target = scheduled_game(
        "target", datetime(2025, 3, 10, 1, tzinfo=UTC), "1823", "Washington", "DC"
    )

    context = travel_context(target, prior_games=(previous, target))

    assert context.distance_miles is not None and context.distance_miles > 1900
    assert context.time_zone_change_hours == 3
    assert context.direction == "east"


def test_travel_context_preserves_missing_prior_game() -> None:
    target = scheduled_game(
        "target", datetime(2025, 3, 10, 1, tzinfo=UTC), "1823", "Washington", "DC"
    )

    context = travel_context(target, prior_games=(target,))

    assert context.distance_miles is None
    assert context.fallback == "no_prior_game"
