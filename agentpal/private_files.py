"""Owner-only permissions for AgentPal's financial SQLite databases (E2E-T4-1).

Ledgers, identities, wallets and rate-limit state must not be readable by other local
users. The file is created 0600 BEFORE SQLite opens it (SQLite gives its -wal/-shm side
files the same mode as the database), and existing files are tightened to 0600.
"""

from __future__ import annotations

import os

PRIVATE_MODE = 0o600


def ensure_private_db(path: str) -> None:
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, PRIVATE_MODE)
        os.close(fd)
    except OSError:
        return  # let sqlite3 raise its own, clearer error on open
    for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            if os.path.exists(candidate) and os.stat(candidate).st_mode & 0o077:
                os.chmod(candidate, PRIVATE_MODE)
        except OSError:
            pass  # not our file (shared deployment): never fail startup for this
