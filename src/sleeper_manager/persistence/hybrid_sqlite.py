"""Local async-shaped storage for the same immutable hybrid contracts used by D1."""

import sqlite3
from pathlib import Path
from typing import Any

from sleeper_manager.persistence.hybrid_runtime import (
    HYBRID_RUNTIME_SCHEMA,
    HybridRuntimeRepository,
)


class _SQLiteHybridSQL:
    """Keep connection scope local to each call and preserve SQLite transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def _first(self, query: str, *params: object) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(query, params).fetchone()
            return dict(row) if row is not None else None

    async def _run(self, query: str, *params: object) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(query, params)


class SQLiteHybridRuntimeRepository(HybridRuntimeRepository):
    """Initialize only the local hybrid tables; the rest of runtime state has its own repository."""

    def __init__(self, path: Path) -> None:
        super().__init__(_SQLiteHybridSQL(path))
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(HYBRID_RUNTIME_SCHEMA)
