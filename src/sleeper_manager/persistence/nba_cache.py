import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import CachedNBARecord, NBADataCache


class SQLiteNBADataCache:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._path)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nba_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    source_updated_at TEXT,
                    expires_at TEXT,
                    quality TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL
                )
                """
            )

    def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cache_key, provider, resource, schema_version, payload_json,
                       retrieved_at, source_updated_at, expires_at, quality,
                       warnings_json, errors_json
                FROM nba_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        record = CachedNBARecord(
            cache_key=row[0],
            provider=row[1],
            resource=row[2],
            schema_version=row[3],
            payload_json=row[4],
            retrieved_at=datetime.fromisoformat(row[5]),
            source_updated_at=datetime.fromisoformat(row[6]) if row[6] else None,
            expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            quality=DataQualityState(row[8]),
            warnings=tuple(json.loads(row[9])),
            errors=tuple(json.loads(row[10])),
        )
        if (
            record.expires_at is not None
            and record.expires_at <= now
            and record.quality in {DataQualityState.FRESH, DataQualityState.PARTIAL}
        ):
            return CachedNBARecord(
                cache_key=record.cache_key,
                provider=record.provider,
                resource=record.resource,
                schema_version=record.schema_version,
                payload_json=record.payload_json,
                retrieved_at=record.retrieved_at,
                source_updated_at=record.source_updated_at,
                expires_at=record.expires_at,
                quality=DataQualityState.STALE,
                warnings=record.warnings + ("Cached record has exceeded its freshness window",),
                errors=record.errors,
            )
        return record

    def put(self, record: CachedNBARecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO nba_cache (
                    cache_key, provider, resource, schema_version, payload_json,
                    retrieved_at, source_updated_at, expires_at, quality,
                    warnings_json, errors_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider = excluded.provider,
                    resource = excluded.resource,
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    retrieved_at = excluded.retrieved_at,
                    source_updated_at = excluded.source_updated_at,
                    expires_at = excluded.expires_at,
                    quality = excluded.quality,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json
                """,
                (
                    record.cache_key,
                    record.provider,
                    record.resource,
                    record.schema_version,
                    record.payload_json,
                    record.retrieved_at.isoformat(),
                    record.source_updated_at.isoformat()
                    if record.source_updated_at is not None
                    else None,
                    record.expires_at.isoformat() if record.expires_at is not None else None,
                    record.quality.value,
                    json.dumps(record.warnings),
                    json.dumps(record.errors),
                ),
            )


__all__: tuple[str, ...] = ("NBADataCache", "SQLiteNBADataCache")
