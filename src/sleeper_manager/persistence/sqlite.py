import sqlite3
from datetime import datetime
from pathlib import Path

from sleeper_manager.persistence.base import StoredLeagueProfile


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS league_profiles (
                    league_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                )
                """
            )

    def load_profile(self, league_id: str) -> StoredLeagueProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT league_id, fingerprint, retrieved_at
                FROM league_profiles
                WHERE league_id = ?
                """,
                (league_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredLeagueProfile(
            league_id=row[0],
            fingerprint=row[1],
            retrieved_at=datetime.fromisoformat(row[2]),
        )

    def save_profile(self, profile: StoredLeagueProfile) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO league_profiles (league_id, fingerprint, retrieved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(league_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    retrieved_at = excluded.retrieved_at
                """,
                (profile.league_id, profile.fingerprint, profile.retrieved_at.isoformat()),
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
