from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sleeper_manager.backtesting.league_archive import (
    HistoricalLeagueArchive,
    HistoricalTransaction,
)

EASTERN_TIME = ZoneInfo("America/New_York")


class RosterTimelineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FantasyWeekBoundary:
    week: int
    leg: int | None
    monday_local: date
    sunday_local: date
    utc_start: datetime
    utc_end: datetime
    season_type: str
    source: str
    confidence: str
    nonstandard_flag: str | None = None

    def __post_init__(self) -> None:
        if self.utc_start.tzinfo is None or self.utc_end.tzinfo is None:
            raise RosterTimelineError("Fantasy-week boundaries must be timezone-aware")
        if self.utc_start >= self.utc_end:
            raise RosterTimelineError("Fantasy-week end must be after start")


@dataclass(frozen=True, slots=True)
class RosterMembershipInterval:
    league_id: str
    roster_id: int
    sleeper_player_id: str
    starts_at: datetime
    ends_at: datetime
    source_transaction_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise RosterTimelineError("Roster interval timestamps must be timezone-aware")
        if self.starts_at >= self.ends_at:
            raise RosterTimelineError("Roster membership intervals must be non-empty")


@dataclass(frozen=True, slots=True)
class RosterTimeline:
    league_id: str
    intervals: tuple[RosterMembershipInterval, ...]
    week_boundaries: tuple[FantasyWeekBoundary, ...]
    exclusions: tuple[str, ...] = ()

    def membership_at(self, roster_id: int, player_id: str, at: datetime) -> bool:
        if at.tzinfo is None:
            raise RosterTimelineError("Membership lookup requires a timezone-aware timestamp")
        return any(
            interval.roster_id == roster_id
            and interval.sleeper_player_id == player_id
            and interval.starts_at <= at < interval.ends_at
            for interval in self.intervals
        )

    def players_at(self, roster_id: int, at: datetime) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    interval.sleeper_player_id
                    for interval in self.intervals
                    if interval.roster_id == roster_id
                    and interval.starts_at <= at < interval.ends_at
                }
            )
        )

    def week_for(self, at: datetime) -> FantasyWeekBoundary | None:
        if at.tzinfo is None:
            raise RosterTimelineError("Week lookup requires a timezone-aware timestamp")
        return next(
            (week for week in self.week_boundaries if week.utc_start <= at < week.utc_end), None
        )


def reconstruct_roster_timeline(
    archive: HistoricalLeagueArchive,
    *,
    week_boundaries: Iterable[FantasyWeekBoundary] = (),
    season_start: datetime | None = None,
    season_end: datetime | None = None,
    weekly_players: Mapping[tuple[int, int], Iterable[str]] | None = None,
) -> RosterTimeline:
    boundaries = tuple(sorted(week_boundaries, key=lambda week: week.utc_start))
    start = season_start or (boundaries[0].utc_start if boundaries else _default_start(archive))
    end = season_end or (boundaries[-1].utc_end if boundaries else _default_end(archive, start))
    _validate_range(start, end)

    roster_players = {roster.roster_id: set(roster.player_ids) for roster in archive.final_rosters}
    exclusions: list[str] = []
    transactions = tuple(
        sorted(
            (transaction for transaction in archive.transactions if transaction.is_complete),
            key=_transaction_sort_key,
        )
    )
    usable_transactions: list[HistoricalTransaction] = []
    for transaction in transactions:
        if transaction.effective_at is None:
            exclusions.append(f"missing_effective_timestamp:{transaction.transaction_id}")
            continue
        usable_transactions.append(transaction)

    for transaction in reversed(usable_transactions):
        for player_id, roster_id in transaction.adds:
            roster_players.setdefault(roster_id, set()).discard(player_id)
        for player_id, roster_id in transaction.drops:
            roster_players.setdefault(roster_id, set()).add(player_id)

    active: dict[tuple[int, str], tuple[datetime, tuple[str, ...]]] = {}
    for roster_id, player_ids in roster_players.items():
        for player_id in sorted(player_ids):
            active[(roster_id, player_id)] = (start, ())

    intervals: list[RosterMembershipInterval] = []
    for transaction in usable_transactions:
        effective_at = transaction.effective_at
        assert effective_at is not None
        if not start <= effective_at <= end:
            exclusions.append(f"transaction_outside_season:{transaction.transaction_id}")
            continue
        for player_id, roster_id in transaction.drops:
            _close_membership(
                active,
                intervals,
                archive.league_id,
                roster_id,
                player_id,
                effective_at,
                transaction.transaction_id,
                exclusions,
            )
        for player_id, roster_id in transaction.adds:
            key = roster_id, player_id
            if key in active:
                exclusions.append(
                    f"duplicate_add:{transaction.transaction_id}:{roster_id}:{player_id}"
                )
                continue
            active[key] = (effective_at, (transaction.transaction_id,))

    for (roster_id, player_id), (starts_at, source_ids) in sorted(active.items()):
        _append_interval(
            intervals,
            RosterMembershipInterval(
                archive.league_id,
                roster_id,
                player_id,
                starts_at,
                end,
                source_ids,
            ),
        )

    _validate_weekly_players(archive, intervals, weekly_players or {}, exclusions, boundaries)
    return RosterTimeline(
        league_id=archive.league_id,
        intervals=tuple(
            sorted(
                intervals, key=lambda item: (item.roster_id, item.starts_at, item.sleeper_player_id)
            )
        ),
        week_boundaries=boundaries,
        exclusions=tuple(sorted(set(exclusions))),
    )


def build_fantasy_week_boundaries(
    week_mondays: Mapping[int, date],
    *,
    season_type: str = "regular",
    legs: Mapping[int, int] | None = None,
    source: str = "archived_matchup_and_schedule",
    confidence: str = "derived",
    nonstandard_weeks: Mapping[int, str] | None = None,
) -> tuple[FantasyWeekBoundary, ...]:
    result: list[FantasyWeekBoundary] = []
    for week, monday in sorted(week_mondays.items()):
        if week <= 0:
            raise RosterTimelineError("Fantasy weeks must be positive")
        if monday.weekday() != 0:
            raise RosterTimelineError(f"Fantasy week {week} does not start on Monday")
        start_local = datetime.combine(monday, time.min, tzinfo=EASTERN_TIME)
        end_local = start_local + timedelta(days=7)
        result.append(
            FantasyWeekBoundary(
                week=week,
                leg=(legs or {}).get(week),
                monday_local=monday,
                sunday_local=monday + timedelta(days=6),
                utc_start=start_local.astimezone(UTC),
                utc_end=end_local.astimezone(UTC),
                season_type=season_type,
                source=source,
                confidence=confidence,
                nonstandard_flag=(nonstandard_weeks or {}).get(week),
            )
        )
    return tuple(result)


def assign_game_to_week(
    game_start: datetime, boundaries: Iterable[FantasyWeekBoundary]
) -> FantasyWeekBoundary | None:
    if game_start.tzinfo is None:
        raise RosterTimelineError("Game start must be timezone-aware")
    return next(
        (week for week in boundaries if week.utc_start <= game_start < week.utc_end),
        None,
    )


def _close_membership(
    active: dict[tuple[int, str], tuple[datetime, tuple[str, ...]]],
    intervals: list[RosterMembershipInterval],
    league_id: str,
    roster_id: int,
    player_id: str,
    ends_at: datetime,
    transaction_id: str,
    exclusions: list[str],
) -> None:
    key = roster_id, player_id
    membership = active.pop(key, None)
    if membership is None:
        exclusions.append(f"drop_without_membership:{transaction_id}:{roster_id}:{player_id}")
        return
    starts_at, source_ids = membership
    if starts_at < ends_at:
        _append_interval(
            intervals,
            RosterMembershipInterval(
                league_id, roster_id, player_id, starts_at, ends_at, source_ids + (transaction_id,)
            ),
        )
    else:
        exclusions.append(f"non_positive_membership:{transaction_id}:{roster_id}:{player_id}")


def _append_interval(
    intervals: list[RosterMembershipInterval], interval: RosterMembershipInterval
) -> None:
    intervals.append(interval)


def _validate_weekly_players(
    archive: HistoricalLeagueArchive,
    intervals: Iterable[RosterMembershipInterval],
    weekly_players: Mapping[tuple[int, int], Iterable[str]],
    exclusions: list[str],
    boundaries: tuple[FantasyWeekBoundary, ...],
) -> None:
    interval_records = tuple(intervals)
    for key, expected_values in weekly_players.items():
        roster_id, week = key
        boundary = next((value for value in boundaries if value.week == week), None)
        if boundary is None:
            exclusions.append(f"missing_week_boundary:{week}")
            continue
        expected = set(expected_values)
        observed = {
            interval.sleeper_player_id
            for interval in interval_records
            if interval.roster_id == roster_id
            and interval.starts_at < boundary.utc_end
            and interval.ends_at > boundary.utc_start
        }
        if expected != observed:
            exclusions.append(
                f"roster_mismatch:week={week}:roster={roster_id}:"
                f"expected={','.join(sorted(expected))}:observed={','.join(sorted(observed))}"
            )


def _transaction_sort_key(transaction: HistoricalTransaction) -> tuple[datetime, str]:
    effective_at = transaction.effective_at
    if effective_at is None:
        return datetime.max.replace(tzinfo=UTC), transaction.transaction_id
    return effective_at, transaction.transaction_id


def _default_start(archive: HistoricalLeagueArchive) -> datetime:
    timestamps = tuple(
        transaction.effective_at
        for transaction in archive.transactions
        if transaction.effective_at is not None
    )
    if timestamps:
        first = min(timestamps)
        return datetime.combine(first.date(), time.min, tzinfo=UTC)
    return datetime(1970, 1, 1, tzinfo=UTC)


def _default_end(archive: HistoricalLeagueArchive, start: datetime) -> datetime:
    timestamps = tuple(
        transaction.effective_at
        for transaction in archive.transactions
        if transaction.effective_at is not None
    )
    last = max(timestamps, default=start)
    return max(start + timedelta(days=7), last + timedelta(days=1))


def _validate_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise RosterTimelineError("Roster timeline range must be timezone-aware")
    if start >= end:
        raise RosterTimelineError("Roster timeline end must be after start")


__all__ = (
    "EASTERN_TIME",
    "FantasyWeekBoundary",
    "RosterMembershipInterval",
    "RosterTimeline",
    "RosterTimelineError",
    "assign_game_to_week",
    "build_fantasy_week_boundaries",
    "reconstruct_roster_timeline",
)
