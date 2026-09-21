"""Minimal SQLite persistence for dojo practice sessions."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path

DEFAULT_OWNER_ID = "local"

CURRENT_OWNER_ID: ContextVar[str] = ContextVar("apprentice_owner_id", default=DEFAULT_OWNER_ID)
"""Owner for the request in flight. Stays ``local`` for single-user installs."""

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_sessions (
  id TEXT PRIMARY KEY,
  incident_id TEXT UNIQUE NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local',
  session_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS practice_sessions_owner
  ON practice_sessions(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS judgment_profile (
  owner_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  headline TEXT NOT NULL,
  bio TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


class SQLiteDatabase:
    """Open a fresh SQLite connection for each managed operation."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValueError("Database requires a file-backed SQLite path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._harden_database_files(create_main=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        self._harden_database_files()
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            self._migrate(connection)
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            connection.close()
            self._harden_database_files()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Move a single-user database onto owner-scoped tables."""
        if connection.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "practice_sessions" in tables:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(practice_sessions)")
            }
            if "owner_id" not in columns:
                connection.execute(
                    "ALTER TABLE practice_sessions ADD COLUMN owner_id TEXT NOT NULL "
                    f"DEFAULT '{DEFAULT_OWNER_ID}'"
                )
        if "judgment_profile" in tables:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(judgment_profile)")
            }
            if "singleton_id" in columns:
                connection.executescript(
                    f"""
                    CREATE TABLE judgment_profile_owned (
                      owner_id TEXT PRIMARY KEY,
                      display_name TEXT NOT NULL,
                      headline TEXT NOT NULL,
                      bio TEXT NOT NULL,
                      updated_at REAL NOT NULL
                    );
                    INSERT INTO judgment_profile_owned(
                      owner_id, display_name, headline, bio, updated_at
                    )
                    SELECT '{DEFAULT_OWNER_ID}', display_name, headline, bio, updated_at
                    FROM judgment_profile;
                    DROP TABLE judgment_profile;
                    ALTER TABLE judgment_profile_owned RENAME TO judgment_profile;
                    """
                )

    def _harden_database_files(self, *, create_main: bool = False) -> None:
        if create_main:
            descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if path.exists():
                # Some platforms and filesystems do not expose POSIX modes.
                with suppress(OSError):
                    path.chmod(0o600)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
