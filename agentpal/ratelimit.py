"""In-memory sliding-window rate limiter (SEC-03).

SINGLE PROCESS ONLY: each worker process has its own counters, so with N uvicorn workers
the effective limit is up to N times the configured one, and counters reset on restart.
Use SQLiteSlidingWindowLimiter for shared local-host admission. Neither backend is
a distributed, multi-host limiter.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000):
        if limit < 1 or window_s <= 0:
            raise ValueError("limit must be >= 1 and window_s > 0")
        self.limit, self.window_s, self.clock, self.max_keys = limit, window_s, clock, max_keys
        self._hits: dict[str, deque[float]] = {}
        self._audited: dict[str, float] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Count one request for `key`. Returns (allowed, retry_after_seconds). A rejected
        request is not counted, so a client that backs off recovers on time."""
        now = self.clock()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._prune(now)
                q = self._hits[key] = deque()
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(1, math.ceil(q[0] + self.window_s - now))
            q.append(now)
            return True, 0

    def should_audit(self, key: str) -> bool:
        """At most one audited rejection per key per window, so a flood of rejected requests
        cannot turn into a flood of audit-log writes."""
        now = self.clock()
        with self._lock:
            last = self._audited.get(key)
            if last is not None and now - last < self.window_s:
                return False
            self._audited[key] = now
            return True

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window_s]:
            del self._hits[k]
        for k in [k for k, t in self._audited.items() if now - t >= self.window_s]:
            del self._audited[k]
        if len(self._hits) >= self.max_keys:
            # still full of active keys: evict the oldest-inserted one to bound memory.
            # Keys are peer addresses and configured token ids, so this needs more than
            # max_keys distinct authenticated tokens or peers to trigger.
            del self._hits[next(iter(self._hits))]


class LimiterUnavailable(RuntimeError):
    """Admission cannot be established. Callers must fail closed, without a fallback."""


class SQLiteSlidingWindowLimiter:
    """Atomic, durable admission across processes sharing LOCAL SQLite storage.

    Opens a connection per operation (no inherited/forked connection). An explicit
    :memory: path is for development/test instances only and is not shared. Wall time
    is clamped against the persisted high-water mark to handle backwards clock steps
    conservatively. A forward clock jump can expire windows; synchronize the host clock.
    """

    def __init__(self, path, namespace: str, limit: int, window_s: float = 60.0,
                 clock: Callable[[], float] = time.time, max_keys: int = 10_000,
                 busy_timeout_s: float = 1.0):
        import sqlite3
        if limit < 1 or window_s <= 0 or max_keys < 1:
            raise ValueError("invalid limiter configuration")
        self.path, self.namespace = str(path), namespace
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self.max_keys, self.busy_timeout_s = max_keys, busy_timeout_s
        self._lock = threading.Lock()
        from .private_files import ensure_private_db
        ensure_private_db(self.path)
        self._memory = (sqlite3.connect(":memory:", isolation_level=None,
                                       check_same_thread=False) if self.path == ":memory:" else None)
        with self._transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS rate_policies (namespace TEXT PRIMARY KEY, "
                      "capacity INTEGER NOT NULL, window REAL NOT NULL, max_keys INTEGER NOT NULL, "
                      "last_now REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS rate_keys (namespace TEXT NOT NULL, "
                      "key TEXT NOT NULL, audited_at REAL, PRIMARY KEY(namespace,key))")
            c.execute("CREATE TABLE IF NOT EXISTS rate_hits (namespace TEXT NOT NULL, "
                      "key TEXT NOT NULL, at REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS rate_hits_key ON rate_hits(namespace,key,at)")
            c.execute("CREATE INDEX IF NOT EXISTS rate_hits_expiry ON rate_hits(namespace,at)")
            c.execute("INSERT OR IGNORE INTO rate_policies VALUES (?,?,?,?,?)",
                      (namespace, limit, window_s, max_keys, float(self.clock())))
            row = c.execute("SELECT capacity,window,max_keys FROM rate_policies WHERE namespace=?",
                            (namespace,)).fetchone()
            if tuple(row) != (limit, window_s, max_keys):
                raise LimiterUnavailable("limiter configuration differs across workers")

    @contextmanager
    def _transaction(self):
        import sqlite3
        with self._lock:
            c = None
            try:
                c = self._memory or sqlite3.connect(self.path, timeout=self.busy_timeout_s,
                                                    isolation_level=None)
                c.execute("PRAGMA synchronous=FULL")
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                if c is not None and c.in_transaction:
                    c.rollback()
                raise LimiterUnavailable("limiter store unavailable") from exc
            except BaseException:
                if c is not None and c.in_transaction:
                    c.rollback()
                raise
            finally:
                if c is not None and c is not self._memory:
                    c.close()

    def _now(self, c):
        now = float(self.clock())
        if not math.isfinite(now):
            raise LimiterUnavailable("invalid limiter clock")
        row = c.execute("SELECT last_now FROM rate_policies WHERE namespace=?",
                        (self.namespace,)).fetchone()
        if row is None:
            raise LimiterUnavailable("missing limiter policy")
        now = max(now, row[0])
        c.execute("UPDATE rate_policies SET last_now=? WHERE namespace=?", (now, self.namespace))
        return now

    def _prune_keys(self, c, now):
        c.execute("DELETE FROM rate_hits WHERE namespace=? AND at<=?",
                  (self.namespace, now - self.window_s))
        c.execute("DELETE FROM rate_keys WHERE namespace=? AND "
                  "(audited_at IS NULL OR audited_at<=?) AND NOT EXISTS "
                  "(SELECT 1 FROM rate_hits h WHERE h.namespace=rate_keys.namespace "
                  "AND h.key=rate_keys.key)", (self.namespace, now - self.window_s))

    def hit(self, key: str) -> tuple[bool, int]:
        with self._transaction() as c:
            now = self._now(c)
            self._prune_keys(c, now)
            known = c.execute("SELECT 1 FROM rate_keys WHERE namespace=? AND key=?",
                              (self.namespace, key)).fetchone()
            if known is None:
                count = c.execute("SELECT count(*) FROM rate_keys WHERE namespace=?",
                                  (self.namespace,)).fetchone()[0]
                if count >= self.max_keys:
                    return False, max(1, math.ceil(self.window_s))
                c.execute("INSERT INTO rate_keys VALUES (?,?,NULL)", (self.namespace, key))
            count, first = c.execute("SELECT count(*),min(at) FROM rate_hits "
                                     "WHERE namespace=? AND key=?", (self.namespace, key)).fetchone()
            if count >= self.limit:
                return False, max(1, math.ceil(first + self.window_s - now))
            c.execute("INSERT INTO rate_hits VALUES (?,?,?)", (self.namespace, key, now))
            return True, 0

    def should_audit(self, key: str) -> bool:
        with self._transaction() as c:
            now = self._now(c)
            # Saturated unknown keys share one audit gate; never allocate unbounded rows.
            row = c.execute("SELECT audited_at FROM rate_keys WHERE namespace=? AND key=?",
                            (self.namespace, key)).fetchone()
            if row is None:
                key = "__capacity__"
                c.execute("INSERT OR IGNORE INTO rate_keys VALUES (?,?,NULL)", (self.namespace, key))
                row = c.execute("SELECT audited_at FROM rate_keys WHERE namespace=? AND key=?",
                                (self.namespace, key)).fetchone()
            if row[0] is not None and now - row[0] < self.window_s:
                return False
            c.execute("UPDATE rate_keys SET audited_at=? WHERE namespace=? AND key=?",
                      (now, self.namespace, key))
            return True
