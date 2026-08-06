from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import CachedNBARecord
from sleeper_manager.persistence.nba_cache import SQLiteNBADataCache


def test_sqlite_cache_marks_expired_records_stale(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cache = SQLiteNBADataCache(tmp_path / "state.db")
    cache.initialize()
    retrieved_at = datetime(2026, 8, 4, 4, tzinfo=UTC)
    cache.put(
        CachedNBARecord(
            cache_key="scoreboard:2026-08-04",
            provider="espn",
            resource="scoreboard",
            schema_version="1",
            payload_json="[]",
            retrieved_at=retrieved_at,
            source_updated_at=None,
            expires_at=retrieved_at + timedelta(minutes=2),
            quality=DataQualityState.FRESH,
        )
    )

    record = cache.get(
        "scoreboard:2026-08-04",
        now=retrieved_at + timedelta(minutes=3),
    )

    assert record is not None
    assert record.quality is DataQualityState.STALE
    assert record.payload_json == "[]"


def test_sqlite_cache_preserves_legitimate_empty_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cache = SQLiteNBADataCache(tmp_path / "state.db")
    cache.initialize()
    retrieved_at = datetime(2026, 8, 4, 4, tzinfo=UTC)
    cache.put(
        CachedNBARecord(
            cache_key="scoreboard:empty",
            provider="espn",
            resource="scoreboard",
            schema_version="1",
            payload_json="[]",
            retrieved_at=retrieved_at,
            source_updated_at=None,
            expires_at=None,
            quality=DataQualityState.EMPTY,
        )
    )

    record = cache.get("scoreboard:empty", now=retrieved_at)

    assert record is not None
    assert record.quality is DataQualityState.EMPTY
