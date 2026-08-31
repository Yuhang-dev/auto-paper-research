"""SQLite-backed LangGraph checkpoint and cross-thread memory storage."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from .config import HarnessSettings, resolve_database_path


class HarnessPersistence:
    """Own two SQLite connections to one persistent database file."""

    def __init__(self, settings: HarnessSettings):
        self.settings = settings
        self.database_path = resolve_database_path(settings.database_path)
        self._checkpoint_connection: Optional[sqlite3.Connection] = None
        self._store_connection: Optional[sqlite3.Connection] = None
        self.checkpointer: Optional[SqliteSaver] = None
        self.store: Optional[SqliteStore] = None

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def open(self) -> "HarnessPersistence":
        if self.checkpointer is not None or self.store is not None:
            return self
        self.settings.ensure_storage_directory()
        self._checkpoint_connection = self._connect(self.database_path)
        self._store_connection = self._connect(self.database_path)
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.store = SqliteStore(self._store_connection)
        self.checkpointer.setup()
        self.store.setup()
        return self

    def close(self) -> None:
        self.checkpointer = None
        self.store = None
        for connection in (self._store_connection, self._checkpoint_connection):
            if connection is not None:
                connection.commit()
                connection.close()
        self._store_connection = None
        self._checkpoint_connection = None

    def checkpoint_counts(self) -> dict:
        if self._checkpoint_connection is None:
            raise RuntimeError("Persistence is not open")
        connection = self._checkpoint_connection
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        result = {"threads": 0, "checkpoints": 0, "writes": 0}
        if "checkpoints" in table_names:
            result["checkpoints"] = connection.execute(
                "SELECT COUNT(*) FROM checkpoints"
            ).fetchone()[0]
            result["threads"] = connection.execute(
                "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
            ).fetchone()[0]
        if "writes" in table_names:
            result["writes"] = connection.execute(
                "SELECT COUNT(*) FROM writes"
            ).fetchone()[0]
        return result

    def __enter__(self) -> "HarnessPersistence":
        return self.open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()
