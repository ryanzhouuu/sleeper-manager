from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from sleeper_manager.domain.nba import ScheduledGame


@dataclass(frozen=True, slots=True)
class VenueLocation:
    latitude: float
    longitude: float
    time_zone: str


@dataclass(frozen=True, slots=True)
class TravelContext:
    prior_venue_id: str | None
    destination_venue_id: str | None
    distance_miles: float | None
    time_zone_change_hours: float | None
    direction: str
    fallback: str


_LOCATIONS = {
    ("atlanta", "ga"): VenueLocation(33.7573, -84.3963, "America/New_York"),
    ("austin", "tx"): VenueLocation(30.2807, -97.7303, "America/Chicago"),
    ("boston", "ma"): VenueLocation(42.3662, -71.0621, "America/New_York"),
    ("brooklyn", "ny"): VenueLocation(40.6826, -73.9754, "America/New_York"),
    ("charlotte", "nc"): VenueLocation(35.2251, -80.8392, "America/New_York"),
    ("chicago", "il"): VenueLocation(41.8807, -87.6742, "America/Chicago"),
    ("cleveland", "oh"): VenueLocation(41.4965, -81.6882, "America/New_York"),
    ("dallas", "tx"): VenueLocation(32.7905, -96.8103, "America/Chicago"),
    ("denver", "co"): VenueLocation(39.7487, -105.0077, "America/Denver"),
    ("detroit", "mi"): VenueLocation(42.3410, -83.0550, "America/Detroit"),
    ("houston", "tx"): VenueLocation(29.7508, -95.3621, "America/Chicago"),
    ("indianapolis", "in"): VenueLocation(39.7640, -86.1555, "America/Indiana/Indianapolis"),
    ("inglewood", "ca"): VenueLocation(33.9535, -118.3392, "America/Los_Angeles"),
    ("las vegas", "nv"): VenueLocation(36.1029, -115.1784, "America/Los_Angeles"),
    ("los angeles", "ca"): VenueLocation(34.0430, -118.2673, "America/Los_Angeles"),
    ("memphis", "tn"): VenueLocation(35.1382, -90.0506, "America/Chicago"),
    ("miami", "fl"): VenueLocation(25.7814, -80.1870, "America/New_York"),
    ("milwaukee", "wi"): VenueLocation(43.0451, -87.9172, "America/Chicago"),
    ("minneapolis", "mn"): VenueLocation(44.9795, -93.2760, "America/Chicago"),
    ("new orleans", "la"): VenueLocation(29.9490, -90.0821, "America/Chicago"),
    ("new york", "ny"): VenueLocation(40.7505, -73.9934, "America/New_York"),
    ("oklahoma city", "ok"): VenueLocation(35.4634, -97.5151, "America/Chicago"),
    ("orlando", "fl"): VenueLocation(28.5392, -81.3839, "America/New_York"),
    ("philadelphia", "pa"): VenueLocation(39.9012, -75.1720, "America/New_York"),
    ("phoenix", "az"): VenueLocation(33.4457, -112.0712, "America/Phoenix"),
    ("portland", "or"): VenueLocation(45.5316, -122.6668, "America/Los_Angeles"),
    ("sacramento", "ca"): VenueLocation(38.5802, -121.4997, "America/Los_Angeles"),
    ("salt lake city", "ut"): VenueLocation(40.7683, -111.9011, "America/Denver"),
    ("san antonio", "tx"): VenueLocation(29.4270, -98.4375, "America/Chicago"),
    ("san francisco", "ca"): VenueLocation(37.7680, -122.3877, "America/Los_Angeles"),
    ("toronto", "on"): VenueLocation(43.6435, -79.3791, "America/Toronto"),
    ("washington", "dc"): VenueLocation(38.8981, -77.0209, "America/New_York"),
}


def venue_location(game: ScheduledGame) -> VenueLocation | None:
    if game.venue_city is None or game.venue_state is None:
        return None
    return _LOCATIONS.get((game.venue_city.casefold(), game.venue_state.casefold()))


def travel_context(
    game: ScheduledGame,
    *,
    prior_games: tuple[ScheduledGame, ...],
) -> TravelContext:
    previous = tuple(record for record in prior_games if record.start_time < game.start_time)
    if not previous:
        return TravelContext(None, game.venue_id, None, None, "unknown", "no_prior_game")
    prior = previous[-1]
    origin = venue_location(prior)
    destination = venue_location(game)
    if origin is None or destination is None:
        return TravelContext(
            prior.venue_id,
            game.venue_id,
            None,
            None,
            "unknown",
            "unknown_venue",
        )
    distance = _great_circle_miles(origin, destination)
    time_zone_change = _utc_offset_hours(destination, game.start_time) - _utc_offset_hours(
        origin, game.start_time
    )
    longitude_change = destination.longitude - origin.longitude
    if abs(longitude_change) < 1:
        direction = "none"
    elif longitude_change > 0:
        direction = "east"
    else:
        direction = "west"
    return TravelContext(
        prior.venue_id,
        game.venue_id,
        round(distance, 3),
        round(time_zone_change, 3),
        direction,
        "observed",
    )


def _great_circle_miles(origin: VenueLocation, destination: VenueLocation) -> float:
    latitude_delta = radians(destination.latitude - origin.latitude)
    longitude_delta = radians(destination.longitude - origin.longitude)
    origin_latitude = radians(origin.latitude)
    destination_latitude = radians(destination.latitude)
    value = sin(latitude_delta / 2) ** 2 + (
        cos(origin_latitude) * cos(destination_latitude) * sin(longitude_delta / 2) ** 2
    )
    return 3958.7613 * 2 * asin(sqrt(value))


def _utc_offset_hours(location: VenueLocation, value: datetime) -> float:
    offset = value.astimezone(ZoneInfo(location.time_zone)).utcoffset()
    if offset is None:
        raise ValueError(f"Could not resolve UTC offset for {location.time_zone}")
    return offset.total_seconds() / 3600
