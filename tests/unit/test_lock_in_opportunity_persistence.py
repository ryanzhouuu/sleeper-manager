"""Live Lock-In opportunity schema and scheduled-work migration."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from test_d1 import FakeD1

NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)


def test_phase_six_migration_preserves_existing_scheduled_work() -> None:
    """Rebuild the constrained work table without losing populated rows."""

    database = FakeD1()
    migrations = Path("infra/cloudflare/migrations")
    asyncio.run(database.exec((migrations / "0001_phase3.sql").read_text()))
    asyncio.run(
        database.exec((migrations / "0002_acknowledged_team_week_decisions.sql").read_text())
    )
    asyncio.run(database.exec((migrations / "0003_scheduled_work.sql").read_text()))
    database.connection.execute(
        """
        INSERT INTO scheduled_work (
            work_id, dedupe_key, kind, due_at, status, created_at, updated_at
        ) VALUES ('existing', 'daily:existing', 'daily', ?, 'pending', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    database.connection.commit()

    asyncio.run(database.exec((migrations / "0004_live_lock_in.sql").read_text()))
    database.connection.execute(
        """
        INSERT INTO scheduled_work (
            work_id, dedupe_key, kind, due_at, status, created_at, updated_at
        ) VALUES ('postgame', 'postgame:game', 'postgame', ?, 'pending', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )

    kinds = tuple(
        row[0]
        for row in database.connection.execute(
            "SELECT kind FROM scheduled_work ORDER BY work_id"
        ).fetchall()
    )
    assert kinds == ("daily", "postgame")
