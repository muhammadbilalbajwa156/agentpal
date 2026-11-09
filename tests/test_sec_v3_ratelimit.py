"""Shared admission is a single budget across independent spawned processes."""
import multiprocessing as mp
import sqlite3

import pytest


def _worker(path, ready, start, results):
    from agentpal.ratelimit import SQLiteSlidingWindowLimiter
    limiter = SQLiteSlidingWindowLimiter(path, "aggregate", 17)
    ready.put(True)
    start.wait(15)
    results.put(sum(limiter.hit("peer")[0] for _ in range(12)))


def test_spawned_processes_share_one_budget(tmp_path):
    from agentpal.ratelimit import SQLiteSlidingWindowLimiter
    path = str(tmp_path / "limits.db")
    SQLiteSlidingWindowLimiter(path, "aggregate", 17)
    ctx = mp.get_context("spawn")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_worker, args=(path, ready, start, results)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        assert sum(results.get(timeout=20) for _ in processes) == 17
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
        ready.close()
        results.close()


def test_restart_window_audit_and_namespace(tmp_path):
    from agentpal.ratelimit import SQLiteSlidingWindowLimiter
    path, now = tmp_path / "limits.db", [100.0]
    def make(namespace="peer"):
        return SQLiteSlidingWindowLimiter(path, namespace, 2, clock=lambda: now[0])
    assert make().hit("k") == (True, 0)
    assert make().hit("k") == (True, 0)
    assert make().hit("k") == (False, 60)
    assert make().should_audit("k")
    assert not make().should_audit("k")
    assert make("token").hit("k")[0]
    now[0] = 90  # backwards clock never resets counters
    assert not make().hit("k")[0]
    now[0] = 160
    assert make().hit("k") == (True, 0)
    assert make().should_audit("k")


def test_active_keys_are_never_evicted_and_capacity_recovers(tmp_path):
    from agentpal.ratelimit import SQLiteSlidingWindowLimiter
    now = [0.0]
    lim = SQLiteSlidingWindowLimiter(tmp_path / "l.db", "peer", 1,
                                     clock=lambda: now[0], max_keys=2)
    assert lim.hit("a")[0] and lim.hit("b")[0]
    assert not lim.hit("c")[0]
    assert not lim.hit("a")[0]
    now[0] = 60
    assert lim.hit("c")[0]


def test_store_failure_and_conflicting_configuration_fail_closed(tmp_path):
    from agentpal.ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
    path = tmp_path / "l.db"
    lim = SQLiteSlidingWindowLimiter(path, "peer", 2, busy_timeout_s=0.01)
    with pytest.raises(LimiterUnavailable):
        SQLiteSlidingWindowLimiter(path, "peer", 3)
    lock = sqlite3.connect(path)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(LimiterUnavailable):
            lim.hit("a")
    finally:
        lock.rollback()
        lock.close()
    assert lim.hit("a")[0]
    path.write_bytes(b"not sqlite")
    with pytest.raises(LimiterUnavailable):
        lim.hit("b")


def test_middleware_fails_503_without_reading_body(tmp_path, monkeypatch):
    import asyncio
    from agentpal.api import SecurityGate
    from agentpal.ratelimit import LimiterUnavailable
    from agentpal.auth import TokenRegistry
    from agentpal.config import Settings
    settings = Settings(auth_mode="required", rate_limit_db_path=str(tmp_path / "l.db"))
    async def forbidden(*args):
        raise AssertionError("body/application reached")
    gate = SecurityGate(forbidden, settings, TokenRegistry(), lambda *args: None)
    monkeypatch.setattr(gate.peer_limiter, "hit", lambda key: (_ for _ in ()).throw(
        LimiterUnavailable("unavailable")))
    sent = []
    async def send(message):
        sent.append(message)
    asyncio.run(gate({"type": "http", "path": "/assess", "client": ("127.0.0.1", 1),
                      "headers": [(b"host", b"localhost")]}, forbidden, send))
    assert sent[0]["status"] == 503


def test_read_only_store_and_injected_insert_failure_never_admit(tmp_path, monkeypatch):
    from agentpal.ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
    path = tmp_path / "l.db"
    limiter = SQLiteSlidingWindowLimiter(path, "peer", 1)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER fail_hit BEFORE INSERT ON rate_hits "
                     "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(LimiterUnavailable):
        limiter.hit("a")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM rate_keys").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_hit")
    original = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original(
            f"file:{path}?mode=ro", uri=True, isolation_level=None))
        with pytest.raises(LimiterUnavailable):
            limiter.hit("a")
    assert limiter.hit("a")[0]
    assert not limiter.hit("a")[0]


def test_initial_store_outage_returns_503_instead_of_reading_request(tmp_path):
    from fastapi.testclient import TestClient
    from agentpal.api import create_app
    from agentpal.config import Settings
    from test_sec_auth import service
    settings = Settings(auth_mode="required", rate_limit_db_path=str(tmp_path / "missing" / "l.db"))
    with TestClient(create_app(service=service(), settings=settings)) as client:
        assert client.get("/health").status_code == 503


def test_saturated_unknown_keys_share_bounded_durable_audit_gate(tmp_path):
    from agentpal.ratelimit import SQLiteSlidingWindowLimiter
    path, now = tmp_path / "audit.db", [100.0]
    def make():
        return SQLiteSlidingWindowLimiter(path, "peer", 1, max_keys=2, clock=lambda: now[0])
    limiter = make()
    assert limiter.hit("active-a")[0] and limiter.hit("active-b")[0]
    assert not limiter.hit("unknown-first")[0]
    assert limiter.should_audit("unknown-first")
    for index in range(25):
        key = f"unknown-{index}"
        assert not limiter.hit(key)[0]
        assert not limiter.should_audit(key)
    assert not make().should_audit("unknown-after-restart")
    with sqlite3.connect(path) as connection:
        # Two active identities and at most one shared saturation-audit row.
        assert connection.execute("SELECT count(*) FROM rate_keys").fetchone()[0] <= 3
        assert connection.execute("SELECT count(*) FROM rate_hits").fetchone()[0] == 2
    assert not limiter.hit("active-a")[0] and not limiter.hit("active-b")[0]
    now[0] = 160.0
    assert limiter.hit("new-window")[0]
    assert limiter.should_audit("new-window")
