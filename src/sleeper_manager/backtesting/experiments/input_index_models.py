"""Validated file references and explicit sample selections for input inventories.

An inventory can retain unbound experiment details; it does not freeze a replay run.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class IndexRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileReference(IndexRecord):
    path: Text
    sha256: Digest


class TeamWeekKey(IndexRecord):
    league_id: Text
    season: Text
    week: PositiveInt
    roster_id: PositiveInt


class WeekBinding(IndexRecord):
    week: PositiveInt
    boundaries_fingerprint: Digest | None = None


class SourceBinding(IndexRecord):
    name: Text
    content_hash: Digest
    version: Text


class LeagueSample(IndexRecord):
    league_id: Text
    season: Text
    role: Literal["primary", "stress"]
    roster_ids: tuple[PositiveInt, ...] = Field(min_length=1)
    weeks: tuple[WeekBinding, ...] = Field(min_length=1)
    scoring_policy_fingerprint: Digest
    league_configuration_fingerprint: Digest
    evidence: tuple[FileReference, ...] = Field(min_length=1)
    shared_sources: tuple[SourceBinding, ...] = ()

    @model_validator(mode="after")
    def unique_scope(self) -> Self:
        """Reject duplicate denominator dimensions and ambiguous shared-source bindings."""
        for values in (
            self.roster_ids,
            tuple(w.week for w in self.weeks),
            tuple(s.name for s in self.shared_sources),
        ):
            if len(set(values)) != len(values):
                raise ValueError("Sample dimensions and source names must be unique")
        return self


class BundleReference(IndexRecord):
    manifest: FileReference
    team_week: FileReference


class FailedAttempt(IndexRecord):
    stage: Literal["before_assembly", "artifact_validation"] = "before_assembly"
    reason: Text
    detail: Text
    evidence: FileReference


class InputSelection(IndexRecord):
    key: TeamWeekKey
    bundle: BundleReference | None = None
    failure: FailedAttempt | None = None
    outputs: tuple[FileReference, ...] = ()

    @model_validator(mode="after")
    def one_attempt(self) -> Self:
        """A failed assembled bundle keeps its own exclusions, not a competing failure record."""
        if (self.bundle is None) == (self.failure is None):
            raise ValueError("Select exactly one bundle or pre-assembly failure")
        return self


class ExperimentInputIndex(IndexRecord):
    schema_version: Literal["experiment-input-index-v1"] = "experiment-input-index-v1"
    protocol: FileReference
    executor_commit: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
    variant: Text
    builder_version: Text
    projection_config_version: Text
    eligibility_policy_version: Text
    policy_configuration: FileReference | None = None
    seeds: tuple[Annotated[int, Field(strict=True, ge=0)], ...] = ()
    unresolved_bindings: tuple[Text, ...] = ()
    leagues: tuple[LeagueSample, ...] = Field(min_length=1)
    selections: tuple[InputSelection, ...] = ()

    @model_validator(mode="after")
    def unique_leagues(self) -> Self:
        """Each league-season has one declared denominator and compatibility contract."""
        keys = tuple((league.league_id, league.season) for league in self.leagues)
        if len(set(keys)) != len(keys):
            raise ValueError("League-season samples must be unique")
        return self
