from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from sleeper_manager.config import ManagerPolicy, load_manager_policy
from sleeper_manager.domain.runtime_policy import RuntimePolicy, default_runtime_policy
from sleeper_manager.persistence.base import (
    AsyncRuntimeStateRepository,
    ProjectionObservationRecord,
    RuntimePolicyRecord,
)
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    compact_direct_baseline_observations,
)


class RuntimeSyncError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CompactRuntimeHistory:
    dataset_version: str
    observation_count: int
    content_hash: str
    records: tuple[ProjectionObservationRecord, ...]


@dataclass(frozen=True, slots=True)
class RuntimeSyncReport:
    apply: bool
    dataset_version: str
    observation_count: int
    content_hash: str
    policy_version: str
    verified_count: int | None = None
    verified_hash: str | None = None
    policy_activated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "apply": self.apply,
            "dataset_version": self.dataset_version,
            "observation_count": self.observation_count,
            "content_hash": self.content_hash,
            "policy_version": self.policy_version,
            "verified_count": self.verified_count,
            "verified_hash": self.verified_hash,
            "policy_activated": self.policy_activated,
        }


def observation_content_hash(records: tuple[ProjectionObservationRecord, ...]) -> str:
    payload = [
        (
            record.history_version,
            record.player_id,
            record.game_id,
            record.game_start.isoformat(),
            record.outcome_finalized_at.isoformat() if record.outcome_finalized_at else None,
            record.minutes if record.minutes is None else float(record.minutes),
            bool(record.started),
            bool(record.did_not_play),
            record.box_score_json,
            record.source_version,
        )
        for record in sorted(
            records,
            key=lambda item: (item.game_start, item.game_id, item.player_id),
        )
    ]
    return sha256(repr(payload).encode("utf-8")).hexdigest()


def records_from_observations(
    observations: tuple[DirectBaselineObservation, ...],
    *,
    history_version: str,
) -> CompactRuntimeHistory:
    records = tuple(
        ProjectionObservationRecord(
            history_version=history_version,
            player_id=observation.player_id,
            game_id=observation.game_id,
            game_start=observation.game_start,
            outcome_finalized_at=observation.outcome_finalized_at,
            minutes=None if observation.minutes is None else float(observation.minutes),
            started=observation.started,
            did_not_play=observation.did_not_play,
            box_score_json=json.dumps(
                asdict(observation.box_score),
                sort_keys=True,
                separators=(",", ":"),
            ),
            source_version=observation.source_version,
        )
        for observation in observations
    )
    return CompactRuntimeHistory(
        dataset_version=history_version,
        observation_count=len(records),
        content_hash=observation_content_hash(records),
        records=records,
    )


def compact_history_from_workspace(
    workspace: Path,
    *,
    league_fixture: Path,
    now: datetime,
) -> CompactRuntimeHistory:
    from sleeper_manager.backtesting.experiments.data import (
        load_historical_experiment_inputs,
        scoring_policy_from_league_fixture,
    )
    from sleeper_manager.backtesting.experiments.feature_validation import (
        _build_dataset,
        _historical_player_ids_by_date_team,
    )
    from sleeper_manager.backtesting.experiments.injuries import acquire_injury_archive

    if now.tzinfo is None:
        raise RuntimeSyncError("Sync timestamp must be timezone-aware")
    scoring_policy = scoring_policy_from_league_fixture(league_fixture)
    inputs = load_historical_experiment_inputs(workspace / "raw", retrieved_at=now)
    injuries = acquire_injury_archive(
        inputs.games,
        inputs.provider_players,
        workspace / "injuries",
        retrieved_at=now,
        historical_player_ids_by_date_team=_historical_player_ids_by_date_team(inputs),
    )
    dataset = _build_dataset(inputs, injuries, scoring_policy, now)
    observations = compact_direct_baseline_observations(dataset.rows)
    if not observations:
        raise RuntimeSyncError("Compact projection history is empty")
    return records_from_observations(observations, history_version=dataset.dataset_version)


def runtime_policy_for_history(
    history: CompactRuntimeHistory,
    *,
    manager_policy: ManagerPolicy | None = None,
    policy_path: Path | None = None,
) -> RuntimePolicy:
    loaded = manager_policy
    if loaded is None and policy_path is not None:
        loaded = load_manager_policy(policy_path)
    manager_policy = loaded if loaded is not None else ManagerPolicy()
    manager_intent = manager_policy.to_manager_intent()
    overrides = dict(manager_policy.players.mapping_overrides)
    base = default_runtime_policy(history_version=history.dataset_version)
    return RuntimePolicy(
        version=base.version,
        manager_timezone=base.manager_timezone,
        daily_plan_time=base.daily_plan_time,
        move_lead_time=base.move_lead_time,
        sleeper_max_age=base.sleeper_max_age,
        availability_max_age=base.availability_max_age,
        team_data_max_age=base.team_data_max_age,
        projection_history_version=base.projection_history_version,
        mapping_overrides=overrides,
        manager_intent=manager_intent,
    )


async def sync_cloudflare_runtime_data(
    history: CompactRuntimeHistory,
    *,
    policy: RuntimePolicy,
    repository: AsyncRuntimeStateRepository | None,
    apply: bool,
    now: datetime,
) -> RuntimeSyncReport:
    if now.tzinfo is None:
        raise RuntimeSyncError("Sync timestamp must be timezone-aware")
    if not apply:
        return RuntimeSyncReport(
            apply=False,
            dataset_version=history.dataset_version,
            observation_count=history.observation_count,
            content_hash=history.content_hash,
            policy_version=policy.version,
        )
    if repository is None:
        raise RuntimeSyncError("Apply requires a D1 repository")
    await repository.save_projection_observations(history.records)
    loaded = await repository.load_projection_observations(history.dataset_version)
    verified_hash = observation_content_hash(loaded)
    if len(loaded) != history.observation_count or verified_hash != history.content_hash:
        raise RuntimeSyncError("Projection history count or hash did not verify")
    await repository.save_runtime_policy(RuntimePolicyRecord(policy.version, policy.to_json(), now))
    return RuntimeSyncReport(
        apply=True,
        dataset_version=history.dataset_version,
        observation_count=history.observation_count,
        content_hash=history.content_hash,
        policy_version=policy.version,
        verified_count=len(loaded),
        verified_hash=verified_hash,
        policy_activated=True,
    )


def redact_secrets(text: str) -> str:
    markers = (
        "CLOUDFLARE_API_TOKEN",
        "NTFY_ACCESS_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "api_token",
        "Bearer ",
    )
    for marker in markers:
        if marker.casefold() in text.casefold():
            return "[redacted]"
    return text


def d1_database_id(wrangler_config: Path) -> str:
    try:
        payload = tomllib.loads(wrangler_config.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeSyncError(f"Could not read {wrangler_config}") from error
    databases = payload.get("d1_databases")
    if not isinstance(databases, list) or not databases:
        raise RuntimeSyncError("wrangler.toml is missing d1_databases")
    database_id = databases[0].get("database_id") if isinstance(databases[0], dict) else None
    if not isinstance(database_id, str) or not database_id.strip():
        raise RuntimeSyncError("wrangler.toml is missing a D1 database_id")
    return database_id.strip()


class RemoteD1Statement:
    def __init__(self, database: RemoteD1, query: str, params: tuple[object, ...] = ()) -> None:
        self.database = database
        self.query = query
        self.params = params

    def bind(self, *params: object) -> RemoteD1Statement:
        return RemoteD1Statement(self.database, self.query, params)

    async def run(self) -> dict[str, Any]:
        return await self.database.execute(self.query, self.params)

    async def first(self) -> dict[str, Any] | None:
        result = await self.all()
        rows = result["results"]
        return rows[0] if rows else None

    async def all(self) -> dict[str, Any]:
        return await self.database.execute(self.query, self.params)


class RemoteD1:
    """Cloudflare D1 HTTP binding used by the local runtime-data sync command."""

    def __init__(
        self,
        account_id: str,
        database_id: str,
        api_token: str,
        client: Any,
    ) -> None:
        if not account_id.strip() or not database_id.strip() or not api_token.strip():
            raise RuntimeSyncError("D1 account, database, and API token are required")
        self._url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/d1/database/{database_id}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self._client = client

    def prepare(self, query: str) -> RemoteD1Statement:
        return RemoteD1Statement(self, query)

    async def exec(self, query: str) -> None:
        await self.execute(query, ())

    async def batch(self, statements: list[RemoteD1Statement]) -> list[dict[str, Any]]:
        return [await statement.run() for statement in statements]

    async def execute(self, sql: str, params: tuple[object, ...]) -> dict[str, Any]:
        response = await self._client.post(
            self._url,
            headers=self._headers,
            json={"sql": sql, "params": list(params)},
        )
        try:
            payload = response.json()
        except Exception as error:
            raise RuntimeSyncError("D1 returned invalid JSON") from error
        if response.status_code >= 400 or not payload.get("success"):
            raise RuntimeSyncError("D1 query failed")
        results = payload.get("result") or []
        first = results[0] if results else {}
        if not isinstance(first, dict):
            first = {}
        return {
            "success": True,
            "results": first.get("results") or [],
            "meta": first.get("meta") or {"changes": 0},
        }


__all__ = (
    "CompactRuntimeHistory",
    "RemoteD1",
    "RuntimeSyncError",
    "RuntimeSyncReport",
    "compact_history_from_workspace",
    "d1_database_id",
    "observation_content_hash",
    "records_from_observations",
    "redact_secrets",
    "runtime_policy_for_history",
    "sync_cloudflare_runtime_data",
)
