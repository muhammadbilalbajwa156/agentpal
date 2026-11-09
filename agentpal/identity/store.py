"""Durable identity persistence (SQLite): organizations, owners, owner credentials.

Only a salted SHA-256 of each credential secret is stored. Credentials are never
deleted: revocation stamps `revoked_at`. The schema version lives in `identity_meta`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from ..private_files import ensure_private_db

IDENTITY_SCHEMA_VERSION = 2


class IdentityStoreError(RuntimeError):
    pass


class IdentityStore:
    def __init__(self, path: str | Path = ":memory:", busy_timeout_s: float = 30.0):
        self.path = str(path)
        self._lock = threading.RLock()
        ensure_private_db(str(self.path))
        self._conn = sqlite3.connect(self.path, timeout=busy_timeout_s,
                                     isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _migrate(self) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS identity_meta (key TEXT PRIMARY KEY, "
                      "value TEXT NOT NULL)")
            row = c.execute("SELECT value FROM identity_meta WHERE key='schema_version'"
                            ).fetchone()
            if row is not None and int(row[0]) > IDENTITY_SCHEMA_VERSION:
                raise IdentityStoreError(f"{self.path} has newer identity schema v{row[0]}")
            for stmt in _SCHEMA:
                c.execute(stmt)
            columns = {r[1] for r in c.execute("PRAGMA table_info(identity_owner_credentials)")}
            for name in ("parent_credential_id", "factor_id", "mfa_verified_at"):
                if name not in columns:
                    c.execute(f"ALTER TABLE identity_owner_credentials ADD COLUMN {name} TEXT")
            c.execute("INSERT OR REPLACE INTO identity_meta (key, value) VALUES "
                      "('schema_version', ?)", (str(IDENTITY_SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS identity_orgs (
        org_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS identity_owners (
        owner_id TEXT PRIMARY KEY,
        org_id TEXT REFERENCES identity_orgs(org_id),
        display_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','disabled')),
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS identity_owner_credentials (
        credential_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES identity_owners(owner_id),
        salt TEXT NOT NULL,
        secret_sha256 TEXT NOT NULL,             -- sha256(salt || secret), hex
        label TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        last_used_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_idc_owner ON identity_owner_credentials(owner_id)",
    """CREATE TABLE IF NOT EXISTS identity_events (       -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id TEXT, credential_id TEXT, action TEXT NOT NULL, at TEXT NOT NULL)""",
]

_SCHEMA.append("""CREATE TABLE IF NOT EXISTS identity_totp (
    owner_id TEXT PRIMARY KEY REFERENCES identity_owners(owner_id),
    factor_id TEXT NOT NULL UNIQUE,
    encrypted_seed BLOB NOT NULL,
    enrollment_session_id TEXT NOT NULL,
    enrollment_expires_at TEXT NOT NULL,
    enabled_at TEXT,
    last_counter INTEGER NOT NULL DEFAULT -1,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT)""")
