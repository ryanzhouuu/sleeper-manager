import sqlite3
from datetime import datetime
from pathlib import Path


class SQLiteStateRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._path)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lock_acknowledgements (
                    recommendation_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL
                )
                """
            )

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO lock_acknowledgements
                    (recommendation_id, player_id, acknowledged_at)
                VALUES (?, ?, ?)
                """,
                (recommendation_id, player_id, acknowledged_at.isoformat()),
            )

    def is_locked(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM lock_acknowledgements WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
        return row is not None
