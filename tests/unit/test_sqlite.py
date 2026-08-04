from datetime import UTC, datetime

from sleeper_manager.persistence.sqlite import SQLiteStateRepository


def test_records_lock_acknowledgement(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()

    repository.record_lock_acknowledgement(
        "recommendation-1",
        "player-1",
        datetime.now(UTC),
    )

    assert repository.is_locked("recommendation-1")
    assert not repository.is_locked("recommendation-2")
