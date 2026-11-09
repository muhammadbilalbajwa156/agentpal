"""Security audit (helper 1), SEC-11: schema upgrades.

The `requests` table gained columns (merchant_domain_input, client_expires_at,
effective_expires_at), and the ledger and trust tables were added, while user_version
stayed 2. `CREATE TABLE IF NOT EXISTS` never adds columns, so a database created at the
first v2 commit (0ab29df) was silently stamped v3 and then failed on every claim (HTTP
500). Upgrades must verify the columns and refuse a database they cannot use.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import ApprovalTerms, ApprovalTermsMismatch, LedgerState, SpendPolicy
from agentpal import store
from agentpal.store import _SCHEMA, AssessmentStore, IncompatibleDatabase

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

EARLY_V2_REQUESTS = """CREATE TABLE requests (
    request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, merchant_name TEXT NOT NULL,
    merchant_domain TEXT, amount TEXT NOT NULL, currency TEXT NOT NULL,
    recurring INTEGER NOT NULL, received_at TEXT NOT NULL, client_created_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('claimed','assessed','assessment_error')),
    recommendation TEXT, assessment_json TEXT, completed_at TEXT)"""


def _version(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_early_v2_database_is_refused_and_not_restamped(tmp_path):
    db = tmp_path / "early_v2.db"
    con = sqlite3.connect(db)
    con.execute(EARLY_V2_REQUESTS)
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    with pytest.raises(IncompatibleDatabase, match="merchant_domain_input"):
        AssessmentStore(db)
    assert _version(db) == 2  # refused databases are left exactly as they were


def test_late_v2_database_upgrades_and_old_requests_fail_closed(tmp_path):
    """The real v2 -> v3 step only added request_terms. Requests assessed before it have no
    terms digest, so they can never be approved (final owner decision 5)."""
    db = tmp_path / "late_v2.db"
    sp = SpendPolicy.model_validate({"policy_version": "t", "agents": {"agent_demo_001": {
        "owner_id": "o", "currencies": {"USD": {
            "monthly_budget": "100.00", "rolling_window_limit": "100.00",
            "per_transaction_hard_limit": "500.00", "per_transaction_review_limit": "90.00"}}}}})
    s = RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()],
                    clock=lambda: T0, spend_policy=sp)
    r = PaymentRequest.model_validate({**VALID, "request_id": "req_v2_old",
                                       "expires_at": (T0 + timedelta(hours=1)).isoformat()})
    s.assess(r)
    s.store.close()
    con = sqlite3.connect(db)  # turn it back into a v2 file
    con.execute("DROP TABLE request_terms")
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()

    s2 = RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()],
                     clock=lambda: T0, spend_policy=sp)
    assert _version(db) == store.SCHEMA_VERSION
    with pytest.raises(ApprovalTermsMismatch):
        s2.store.record_owner_decision("req_v2_old", "approved", "owner",
                                       terms=ApprovalTerms.from_request(r), now=T0)
    assert s2.store.ledger_state("req_v2_old") == LedgerState.reserved


def test_every_current_table_is_column_checked(tmp_path):
    """A table that exists but lacks a column the code writes is refused, for each table."""
    for stmt in _SCHEMA:
        if not stmt.lstrip().startswith("CREATE TABLE"):
            continue
        name = stmt.split("EXISTS", 1)[1].split("(", 1)[0].strip()
        db = tmp_path / f"{name}.db"
        con = sqlite3.connect(db)
        con.execute(f"CREATE TABLE {name} (unrelated TEXT)")
        con.execute("PRAGMA user_version=2")
        con.commit()
        con.close()
        with pytest.raises(IncompatibleDatabase, match=name):
            AssessmentStore(db)


def test_fresh_and_current_databases_still_open(tmp_path):
    db = tmp_path / "fresh.db"
    AssessmentStore(db).close()
    AssessmentStore(db).close()
    assert _version(db) == store.SCHEMA_VERSION
