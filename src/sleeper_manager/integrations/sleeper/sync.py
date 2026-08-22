import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from sleeper_manager.domain.league import (
    FantasyWeek,
    LeagueMode,
    LeagueProfile,
    LeagueTransaction,
    LeagueUser,
    RosterSlot,
)
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.scoring import ScoringCompatibilityError, ScoringPolicy
from sleeper_manager.integrations.sleeper.schemas import (
    SleeperLeaguePayload,
    SleeperRosterPayload,
    SleeperStatePayload,
    SleeperTransactionPayload,
    SleeperUserPayload,
)
from sleeper_manager.persistence.base import LeagueProfileStore, StoredLeagueProfile


class LeagueBootstrapError(RuntimeError):
    """Base error for a league that cannot be safely initialized."""


class SleeperPayloadError(LeagueBootstrapError):
    """Raised when a provider payload does not match the expected shape."""


class LeagueCompatibilityError(LeagueBootstrapError):
    """Raised when Sleeper reports unsupported league configuration."""


class LeagueOwnershipError(LeagueBootstrapError):
    """Raised when the configured user cannot be mapped to exactly one roster."""


class SleeperReader(Protocol):
    async def league(self, league_id: str) -> dict[str, Any]: ...

    async def rosters(self, league_id: str) -> list[dict[str, Any]]: ...

    async def users(self, league_id: str) -> list[dict[str, Any]]: ...

    async def state(self) -> dict[str, Any]: ...

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class LeagueSyncResult:
    profile: LeagueProfile
    configuration_changed: bool
    previous_fingerprint: str | None


_SUPPORTED_ROSTER_SLOTS = frozenset({"PG", "SG", "G", "SF", "PF", "F", "C", "UTIL", "BN", "IR"})
_RESERVE_SLOTS = frozenset({"BN", "IR"})
_MODE_PATHS = (
    ("mode",),
    ("game_mode",),
    ("lock_in_mode",),
    ("settings", "mode"),
    ("settings", "game_mode"),
    ("settings", "league_mode"),
    ("settings", "type"),
)
_NUMERIC_MODE_PATHS = frozenset({("game_mode",), ("settings", "game_mode")})


def _parse_payload[ModelT: BaseModel](
    model: type[ModelT], payload: Mapping[str, Any], name: str
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise SleeperPayloadError(f"Invalid Sleeper {name} payload: {error}") from error


def _mode_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _parse_mode(payload: Mapping[str, Any]) -> LeagueMode:
    found_paths: list[str] = []
    for path in _MODE_PATHS:
        value = _mode_value(payload, path)
        if value is None:
            continue
        found_paths.append(".".join(path))
        if isinstance(value, bool) and value:
            return LeagueMode.LOCK_IN
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and path in _NUMERIC_MODE_PATHS
            and value == 1
        ):
            return LeagueMode.LOCK_IN
        if isinstance(value, str):
            normalized = value.casefold().replace("-", "_").replace(" ", "_")
            if normalized in {"lock_in", "lockin"}:
                return LeagueMode.LOCK_IN

    checked = ", ".join(found_paths) if found_paths else "known mode fields"
    raise LeagueCompatibilityError(
        "Sleeper league does not expose a verified Lock-In mode marker "
        f"(checked {checked}); refusing to infer league mode"
    )


def _parse_slots(values: list[str]) -> tuple[RosterSlot, ...]:
    slots: list[RosterSlot] = []
    for index, value in enumerate(values):
        position = value.strip().upper()
        if position not in _SUPPORTED_ROSTER_SLOTS:
            raise LeagueCompatibilityError(f"Unsupported Sleeper roster slot: {value!r}")
        slots.append(
            RosterSlot(index=index, position=position, is_starting=position not in _RESERVE_SLOTS)
        )
    if not any(slot.is_starting for slot in slots):
        raise LeagueCompatibilityError("League has no starting roster slots")
    return tuple(slots)


def _ids(values: list[str]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip() and value.strip() != "0")


def _starter_ids(values: list[str]) -> tuple[str | None, ...]:
    result: list[str | None] = []
    for value in values:
        normalized = value.strip()
        result.append(normalized if normalized and normalized != "0" else None)
    return tuple(result)


def _parse_roster(payload: SleeperRosterPayload) -> Roster:
    players = _ids(payload.players)
    starters = _starter_ids(payload.starters)
    reserve = _ids(payload.reserve or [])
    concrete_starters = tuple(starter for starter in starters if starter is not None)
    if not set(concrete_starters).issubset(players):
        missing = sorted(set(concrete_starters) - set(players))
        raise LeagueCompatibilityError(
            f"Roster {payload.roster_id} lists starters not present in players: {missing}"
        )
    return Roster(
        roster_id=payload.roster_id,
        owner_id=payload.owner_id,
        player_ids=players,
        starter_ids=starters,
        reserve_ids=reserve,
    )


def _parse_user(payload: SleeperUserPayload) -> LeagueUser:
    team_name = payload.metadata.get("team_name")
    return LeagueUser(
        sleeper_id=payload.user_id,
        username=payload.username,
        display_name=payload.display_name,
        team_name=team_name if isinstance(team_name, str) else None,
    )


def _int_value(value: int | str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SleeperPayloadError(f"Sleeper {field} contains a non-integer value") from error


def _parse_changes(values: Mapping[str, int | str] | None) -> tuple[tuple[str, int], ...]:
    if not values:
        return ()
    return tuple(
        sorted(
            (str(player_id), _int_value(roster_id, "transaction roster"))
            for player_id, roster_id in values.items()
        )
    )


def _parse_transaction(payload: SleeperTransactionPayload) -> LeagueTransaction:
    return LeagueTransaction(
        transaction_id=payload.transaction_id,
        transaction_type=payload.type,
        status=payload.status,
        leg=payload.leg,
        roster_ids=tuple(_int_value(value, "transaction roster") for value in payload.roster_ids),
        adds=_parse_changes(payload.adds),
        drops=_parse_changes(payload.drops),
    )


def _parse_week(
    state: SleeperStatePayload,
    *,
    season: str,
    season_type: str,
) -> FantasyWeek:
    week = next(
        (value for value in (state.week, state.leg, state.display_week) if value is not None),
        None,
    )
    if week is None or week < 0:
        raise LeagueCompatibilityError("Sleeper NBA state does not contain a valid fantasy week")
    resolved_season_type = state.season_type or season_type
    if week == 0 and resolved_season_type not in {"off", "pre"}:
        raise LeagueCompatibilityError(
            "Sleeper NBA state reported week 0 with unsupported season type "
            f"{resolved_season_type!r}"
        )
    return FantasyWeek(
        week=week,
        season=state.season or season,
        season_type=resolved_season_type,
    )


def _configuration_fingerprint(
    payload: Mapping[str, Any],
    *,
    mode: LeagueMode,
) -> str:
    canonical = {
        "league_id": payload.get("league_id"),
        "sport": payload.get("sport"),
        "season": payload.get("season"),
        "season_type": payload.get("season_type"),
        "total_rosters": payload.get("total_rosters"),
        "previous_league_id": payload.get("previous_league_id"),
        "roster_positions": payload.get("roster_positions"),
        "scoring_settings": payload.get("scoring_settings"),
        "settings": payload.get("settings", {}),
        "mode": mode.value,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


class LeagueSynchronizationService:
    """Builds a validated, provider-independent league profile."""

    def __init__(
        self,
        sleeper: SleeperReader,
        *,
        profile_store: LeagueProfileStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._sleeper = sleeper
        self._profile_store = profile_store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def sync(self, *, league_id: str, user_id: str) -> LeagueSyncResult:
        previous = (
            self._profile_store.load_profile(league_id) if self._profile_store is not None else None
        )
        raw_league = await self._sleeper.league(league_id)
        league = _parse_payload(SleeperLeaguePayload, raw_league, "league")
        if league.league_id != league_id:
            raise LeagueCompatibilityError(
                f"Sleeper returned league {league.league_id!r} for requested league {league_id!r}"
            )
        if league.sport.casefold() != "nba":
            raise LeagueCompatibilityError(f"Unsupported Sleeper sport: {league.sport!r}")

        mode = _parse_mode(raw_league)
        try:
            scoring = ScoringPolicy.from_sleeper(league.scoring_settings)
        except ScoringCompatibilityError as error:
            raise LeagueCompatibilityError(str(error)) from error
        roster_slots = _parse_slots(league.roster_positions)

        raw_users = await self._sleeper.users(league_id)
        raw_rosters = await self._sleeper.rosters(league_id)
        raw_state = await self._sleeper.state()
        users = tuple(
            _parse_user(_parse_payload(SleeperUserPayload, payload, "user"))
            for payload in raw_users
        )
        rosters = tuple(
            _parse_roster(_parse_payload(SleeperRosterPayload, payload, "roster"))
            for payload in raw_rosters
        )
        matching_users = tuple(user for user in users if user.sleeper_id == user_id)
        if len(matching_users) != 1:
            raise LeagueOwnershipError(
                f"Configured Sleeper user {user_id!r} matched {len(matching_users)} league users"
            )
        matching_rosters = tuple(roster for roster in rosters if roster.owner_id == user_id)
        if len(matching_rosters) != 1:
            raise LeagueOwnershipError(
                f"Configured Sleeper user {user_id!r} owns {len(matching_rosters)} league rosters"
            )

        state = _parse_payload(SleeperStatePayload, raw_state, "NBA state")
        fantasy_week = _parse_week(state, season=league.season, season_type=league.season_type)
        raw_transactions = (
            await self._sleeper.transactions(league_id, fantasy_week.week)
            if fantasy_week.week > 0
            else []
        )
        transactions = tuple(
            _parse_transaction(_parse_payload(SleeperTransactionPayload, payload, "transaction"))
            for payload in raw_transactions
        )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise ValueError("League synchronization clock must return a timezone-aware datetime")
        fingerprint = _configuration_fingerprint(raw_league, mode=mode)
        profile = LeagueProfile(
            league_id=league.league_id,
            name=league.name,
            sport=league.sport,
            season=league.season,
            season_type=league.season_type,
            status=league.status,
            total_rosters=league.total_rosters,
            previous_league_id=league.previous_league_id,
            mode=mode,
            roster_slots=roster_slots,
            scoring=scoring,
            users=users,
            rosters=rosters,
            manager_user_id=user_id,
            manager_roster_id=matching_rosters[0].roster_id,
            fantasy_week=fantasy_week,
            transactions=transactions,
            configuration_fingerprint=fingerprint,
            retrieved_at=retrieved_at,
        )
        if self._profile_store is not None:
            self._profile_store.save_profile(
                StoredLeagueProfile(
                    league_id=profile.league_id,
                    fingerprint=profile.configuration_fingerprint,
                    retrieved_at=profile.retrieved_at,
                )
            )
        return LeagueSyncResult(
            profile=profile,
            configuration_changed=previous is not None
            and previous.fingerprint != profile.configuration_fingerprint,
            previous_fingerprint=previous.fingerprint if previous is not None else None,
        )
