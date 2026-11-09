"""Offline acceptance through real CLI processes and real TCP/SQLite."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import sqlite3
import time

import httpx
import pytest

from .support import Server, free_port


@pytest.fixture
def server(tmp_path):
    instance = Server(tmp_path).start()
    try:
        yield instance
    finally:
        instance.close()


def test_sigkill_approved_request_preserves_exact_hold_and_approval(server):
    aid, key, _wallet = server.agent()
    payment = server.payment(key)
    server.approve(payment)
    before = server.allowance(key)
    server.restart()
    actual = server.call("GET", "/v1/payment-requests/" + payment["request_id"]).json()
    assert actual["status"] == "approved"
    assert actual["terms_sha256"] == payment["terms_sha256"]
    assert server.allowance(key) == before
    approval = server.ledger("SELECT * FROM approvals WHERE agent_id=?", (aid,))
    assert len(approval) == 1 and approval[0]["consumed_at"] is None


def test_parallel_http_creation_reserves_once_and_conflict_is_safe(server):
    aid, key, _wallet = server.agent()
    payload = {"amount": "1.00", "currency": "USD", "merchant_name": "T4 Test Store",
               "purpose": "Same logical request"}
    headers = {"Authorization": "Bearer " + key, "Idempotency-Key": "t4-concurrent-same"}
    def create(_):
        return server.client.post("/v1/payment-requests", headers=headers, json=payload)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(create, range(48)))
    assert sum(r.status_code == 201 for r in results) == 1
    assert all(r.status_code in (200, 201, 409) for r in results)
    assert len({r.json()["request_id"] for r in results if r.status_code in (200, 201)}) == 1
    assert Decimal(server.allowance(key)["committed"]) == Decimal("1")
    assert len(server.ledger("SELECT * FROM ledger_entries WHERE agent_id=?", (aid,))) == 1
    conflict = server.client.post("/v1/payment-requests", headers=headers,
                                  json={**payload, "amount": "2.00"})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_key_reused"


def test_concurrent_execution_claim_is_single_use_without_false_confirmation(server):
    aid, key, _wallet = server.agent()
    payment = server.payment(key)
    server.approve(payment)
    path = "/v1/payment-requests/" + payment["request_id"] + "/execute"
    def execute(_):
        return server.client.post(path, headers={"Authorization": "Bearer " + key})
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(execute, range(24)))
    assert all(r.status_code in (200, 409) for r in results)
    assert all(r.json().get("status") != "provider_confirmed" for r in results)
    assert len(server.ledger("SELECT * FROM payment_attempts WHERE agent_id=?", (aid,))) == 1
    assert len(server.ledger("SELECT * FROM provider_operations WHERE agent_id=?", (aid,))) == 1
    server.restart()
    assert Decimal(server.allowance(key)["committed"]) == Decimal("1")
    assert server.call("GET", path.removesuffix("/execute")).json()["status"] == "reconciliation_required"


def test_rate_limit_survives_sigkill_with_same_settings(tmp_path):
    server = Server(tmp_path, token_limit=8).start()
    try:
        _aid, key, _wallet = server.agent()
        headers = {"Authorization": "Bearer " + key}
        codes = [server.client.get("/v1/me", headers=headers).status_code for _ in range(10)]
        assert codes == [200] * 8 + [429] * 2
        server.restart()
        denied = server.client.get("/v1/me", headers=headers)
        assert denied.status_code == 429 and int(denied.headers["Retry-After"]) > 0
    finally:
        server.close()


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T4-1).
def test_financial_databases_are_private_under_standard_umask(tmp_path):
    previous = os.umask(0o022)
    server = None
    try:
        server = Server(tmp_path).start()
        insecure = {p.name: oct(p.stat().st_mode & 0o777) for p in tmp_path.glob("*.db")
                    if p.stat().st_mode & 0o077}
        assert not insecure, f"Financial databases readable by other local users: {insecure}"
    finally:
        if server:
            server.close()
        os.umask(previous)


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T4-2).
def test_identity_database_contention_does_not_stall_health(server):
    # Holding the real identity write lock simulates maintenance/another process.
    lock = sqlite3.connect(server.directory / "identity.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(server.call, "GET", "/v1/agents", expected=(200, 503))
    try:
        time.sleep(.2)
        try:
            response = server.client.get("/v1/health", timeout=.7)
            responsive = response.status_code == 200
        except httpx.TimeoutException:
            responsive = False
    finally:
        lock.rollback()
        lock.close()
        future.result(timeout=5)
        pool.shutdown()
    assert responsive, "Identity SQLite lock blocks the ASGI event loop and liveness"


@pytest.mark.parametrize("overrides", [
    {"AGENTPAL_RATE_LIMIT_PER_TOKEN": "not-an-integer"},
    {"AGENTPAL_DB_PATH": ":memory:", "AGENTPAL_ENV": "production"},
    {"PAYPAL_SANDBOX_CLIENT_ID": "synthetic-id-with-no-secret"},
    {"AGENTPAL_REQUIRE_MFA_FOR_APPROVAL": "invalid-boolean"},
])
def test_invalid_startup_configuration_refuses_before_binding(tmp_path, overrides):
    import subprocess
    import sys
    from .support import ROOT, environment
    env = {**environment(tmp_path), **overrides}
    result = subprocess.run([sys.executable, "-m", "agentpal", "serve", "--platform",
                             "--port", str(free_port())], cwd=ROOT, env=env, capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b"Application startup complete" not in result.stdout + result.stderr


def test_newer_wallet_schema_refuses_startup_without_rewriting_it(server):
    server.stop()
    with sqlite3.connect(server.directory / "wallet.db") as conn:
        conn.execute("UPDATE wallet_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(RuntimeError, match="startup failed"):
        server.start()
    assert b"newer wallet schema v999" in server.logs
    with sqlite3.connect(server.directory / "wallet.db") as conn:
        assert conn.execute("SELECT value FROM wallet_meta WHERE key='schema_version'").fetchone()[0] == "999"


def test_corrupt_ledger_fails_closed_on_restart(server):
    _aid, key, _wallet = server.agent()
    server.payment(key)
    server.stop()
    with sqlite3.connect(server.directory / "ledger.db") as conn:
        conn.execute("UPDATE ledger_entries SET amount='1.01'")
    with pytest.raises(RuntimeError, match="startup failed"):
        server.start()
    assert b"ConsistencyError" in server.logs
