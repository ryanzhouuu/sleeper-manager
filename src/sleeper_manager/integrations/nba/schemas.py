from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ESPNScoreboardPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[dict[str, Any]] = Field(default_factory=list)


class ESPNGameSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    header: dict[str, Any]
    boxscore: dict[str, Any] = Field(default_factory=dict)


class ESPNInjuriesPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    injuries: list[dict[str, Any]] = Field(default_factory=list)


class ESPNTeamRosterPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    athletes: list[dict[str, Any]] = Field(default_factory=list)


class ESPNTeamSchedulePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[dict[str, Any]] = Field(default_factory=list)
