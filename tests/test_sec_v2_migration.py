"""SEC-13: tested migration of a schema-v3 database (global request_id) to v4
((agent_id, request_id) keys, approvals, provider orders and events).

The v3 file is built here from a verbatim copy of the v3 DDL, independent of the store.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentpal import store as store_mod
from agentpal.spending import ApprovalNotUsable, ApprovalTerms, LedgerState
from agentpal.store import AssessmentStore, IncompatibleDatabase

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

V3_DDL = [
    """CREATE TABLE requests (request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
        merchant_name TEXT NOT NULL, merchant_domain TEXT, merchant_domain_input TEXT,
        amount TEXT NOT NULL, currency TEXT NOT NULL, recurring INTEGER NOT NULL,
        received_at TEXT NOT NULL, client_created_at TEXT, client_expires_at TEXT,
        effective_expires_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('claimed','assessed','assessment_error')),
        recommendation TEXT, assessment_json TEXT, completed_at TEXT)""",
    "CREATE INDEX idx_requests_agent ON requests(agent_id, received_at)",
    """CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        agent_id TEXT, timestamp TEXT NOT NULL, status TEXT NOT NULL, model_backend TEXT,
        model_name TEXT, model_version TEXT, is_fallback INTEGER, rules_version TEXT,
        risk_score INTEGER, risk_category TEXT, recommendation TEXT, signal_codes TEXT,
        errors TEXT)""",
    "CREATE INDEX idx_audit_req ON audit_log(request_id)",
    """CREATE TABLE ledger_entries (request_id TEXT PRIMARY KEY REFERENCES
        requests(request_id), agent_id TEXT NOT NULL, owner_id TEXT, merchant_key TEXT NOT
        NULL, currency TEXT NOT NULL, amount TEXT NOT NULL, state TEXT NOT NULL CHECK (state
        IN ('reserved','approved','pending','completed','released','failed','cancelled')),
        period TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    "CREATE INDEX idx_ledger_agent ON ledger_entries(agent_id, currency, state)",
    """CREATE TABLE owner_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT
        NOT NULL, decision TEXT NOT NULL CHECK (decision IN ('approved','declined')),
        decided_by TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE payment_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT
        NOT NULL, provider TEXT NOT NULL, provider_reference TEXT NOT NULL, at TEXT NOT
        NULL)""",
    """CREATE TABLE payment_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT
        NOT NULL, outcome TEXT NOT NULL CHECK (outcome IN ('completed','failed','cancelled')),
        verified INTEGER NOT NULL CHECK (verified IN (0,1)), verification_method TEXT NOT
        NULL, provider_reference TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE request_terms (request_id TEXT PRIMARY KEY REFERENCES
        requests(request_id), terms_sha256 TEXT NOT NULL)""",
    """CREATE TABLE ledger_events (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT
        NULL, from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT,
        at TEXT NOT NULL)""",
]

TERMS = ApprovalTerms(request_id="req_old_1", agent_id="agent_a", amount="49.00",
                      currency="USD", merchant_name="Example Hosting",
                      merchant_domain="example.com", purpose="Monthly hosting")


def build_v3(path, orphan=False):
    con = sqlite3.connect(path)
    for stmt in V3_DDL:
        con.execute(stmt)
    t, exp = T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()
    for rid, agent, state in (("req_old_1", "agent_a", "approved"),
                              ("req_old_2", "agent_b", "completed")):
        con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, agent, "Example Hosting", "example.com", None, "49.00", "USD", 0,
                     t, None, None, exp, "assessed", "allow_for_review", "{}", t))
        con.execute("INSERT INTO request_terms VALUES (?,?)",
                    (rid, TERMS.digest() if rid == "req_old_1" else "0" * 64))
        con.execute("INSERT INTO ledger_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, agent, "owner_x", "domain:example.com", "USD", "49.00", state,
                     "2026-10", t, t, exp))
        con.execute("INSERT INTO ledger_events (request_id, from_state, to_state, actor, "
                    "reason, at) VALUES (?,?,?,?,?,?)",
                    (rid, None, "reserved", "system", "request_within_limits", t))
        con.execute("INSERT INTO owner_decisions (request_id, decision, decided_by, at) "
                    "VALUES (?,?,?,?)", (rid, "approved", "owner_x", t))
    con.execute("INSERT INTO payment_attempts (request_id, provider, provider_reference, at) "
                "VALUES ('req_old_2','sandbox','att-1',?)", (t,))
    con.execute("INSERT INTO payment_outcomes (request_id, outcome, verified, "
                "verification_method, provider_reference, at) VALUES "
                "('req_old_2','completed',1,'test','cap-1',?)", (t,))
    con.execute("INSERT INTO audit_log (request_id, agent_id, timestamp, status) VALUES "
                "('req_old_1','agent_a',?,'assessed')", (t,))
    if orphan:
        con.execute("INSERT INTO ledger_events (request_id, to_state, actor, at) VALUES "
                    "('req_gone','reserved','system',?)", (t,))
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()


def version(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_v3_database_migrates_to_v4_preserving_everything(tmp_path):
    db = tmp_path / "v3.db"
    build_v3(db)
    s = AssessmentStore(db)
    s.clock = lambda: T0
    assert version(db) == store_mod.SCHEMA_VERSION == 4
    assert s.ledger_state("req_old_1") == LedgerState.approved
    assert s.ledger_state("req_old_2", agent_id="agent_b") == LedgerState.completed
    assert [e["agent_id"] for e in s.ledger_events("req_old_1")] == ["agent_a"]
    assert s.owner_decision_records("req_old_2")[0]["agent_id"] == "agent_b"
    trail = s.trust_trail("req_old_2")
    assert {t["kind"] for t in trail} == {"owner_decision", "payment_attempt",
                                          "payment_outcome"}
    assert len(s.trusted_history_for("agent_b", "owner_x")) == 1
    assert s.audit_records("req_old_1")[0]["status"] == "assessed"
    pk = [r["name"] for r in s.query("SELECT name, pk FROM pragma_table_info('requests') "
                                     "WHERE pk > 0 ORDER BY pk")]
    assert pk == ["agent_id", "request_id"]
    # a legacy v3 approval has no structured, authenticated approval record: never executable
    with pytest.raises(ApprovalNotUsable):
        s.begin_execution("req_old_1", TERMS, "paypal", agent_id="agent_a", now=T0)
    assert s.ledger_state("req_old_1") == LedgerState.approved
    s.close()
    AssessmentStore(db).close()  # v4 reopens unchanged
    assert version(db) == 4


def test_v3_with_orphan_rows_is_refused_and_left_untouched(tmp_path):
    db = tmp_path / "orphan.db"
    build_v3(db, orphan=True)
    with pytest.raises(IncompatibleDatabase, match="ledger_events"):
        AssessmentStore(db)
    assert version(db) == 3
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "approvals" not in tables and "requests_v3" not in tables


def test_newer_schema_is_refused(tmp_path):
    db = tmp_path / "v5.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=5")
    con.commit()
    con.close()
    with pytest.raises(IncompatibleDatabase, match="newer"):
        AssessmentStore(db)
