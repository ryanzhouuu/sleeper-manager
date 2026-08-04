from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SleeperLeaguePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    league_id: str
    name: str = ""
    sport: str
    season: str
    season_type: str
    status: str
    total_rosters: int
    previous_league_id: str | None = None
    roster_positions: list[str]
    scoring_settings: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)


class SleeperUserPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: str
    username: str | None = None
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SleeperRosterPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    roster_id: int
    owner_id: str | None = None
    players: list[str] = Field(default_factory=list)
    starters: list[str] = Field(default_factory=list)
    reserve: list[str] = Field(default_factory=list)


class SleeperStatePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    week: int | None = None
    leg: int | None = None
    display_week: int | None = None
    season: str | None = None
    season_type: str | None = None


class SleeperTransactionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    type: str
    status: str
    leg: int | None = None
    roster_ids: list[int | str] = Field(default_factory=list)
    adds: dict[str, int | str] | None = None
    drops: dict[str, int | str] | None = None
