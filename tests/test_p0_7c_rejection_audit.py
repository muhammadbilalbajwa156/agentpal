"""P0.7c regression: rejected requests (422/413/400) produce structured audit events
without storing raw request content, and audit failures never change the response.

Before the fix, validation failures were rejected before reaching the service and left
no audit record at all.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.config import Settings, load_policy
from agentpal.service import RiskService
from agentpal.store import AssessmentStore

from conftest import VALID

SECRET = "sk_live_ABCDEF1234567890SECRET"


def setup(max_bytes=8192):
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()])
    return s, TestClient(create_app(service=s, settings=Settings(max_request_bytes=max_bytes)))


def rejections(s):
    return [r for r in s.store.audit_records() if r["status"].startswith("rejected:")]


def test_validation_failure_is_audited_with_codes_only():
    s, c = setup()
    with c:
        r = c.post("/assess", json={**VALID, "amount": -5})
    assert r.status_code == 422
    rows = rejections(s)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "rejected:validation"
    assert row["request_id"] == VALID["request_id"] and row["agent_id"] == VALID["agent_id"]
    details = json.loads(row["errors"])
    assert details == [{"loc": ["body", "amount"], "type": "value_error"}]


def test_invalid_identifiers_are_not_stored_verbatim():
    s, c = setup()
    with c:
        c.post("/assess", json={**VALID, "request_id": "bad id; DROP TABLE", "agent_id": 5})
    row = rejections(s)[0]
    assert row["request_id"] == "unknown" and row["agent_id"] is None


def test_secrets_in_rejected_payloads_are_never_stored_logged_or_echoed(capsys):
    s, c = setup()
    with c:
        r = c.post("/assess", json={**VALID, "amount": SECRET, "currency": SECRET,
                                    "business_justification": f"api_key={SECRET}"})
    assert r.status_code == 422
    stored = json.dumps(s.store.audit_records())
    logged = capsys.readouterr()
    for haystack in (stored, r.text, logged.out, logged.err):
        assert SECRET.lower() not in haystack.lower()  # also catches upper-cased echoes


def test_malformed_json_is_audited():
    s, c = setup()
    with c:
        r = c.post("/assess", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    row = rejections(s)[0]
    assert row["status"] == "rejected:validation" and row["request_id"] == "unknown"
    assert json.loads(row["errors"])[0]["type"] == "json_invalid"


def test_oversized_body_is_audited():
    s, c = setup(max_bytes=1024)
    with c:
        r = c.post("/assess", content=b"x" * 5000, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert [x["status"] for x in rejections(s)] == ["rejected:request_too_large"]


def test_audit_failure_does_not_change_response_or_leak(capsys, monkeypatch):
    s, c = setup()

    def broken(*a, **k):
        raise RuntimeError(f"db down {SECRET}")

    monkeypatch.setattr(s.store, "record_rejection", broken)
    with c:
        r = c.post("/assess", json={**VALID, "amount": -1})
        big = c.post("/assess", content=b"x" * 9000, headers={"content-type": "application/json"})
    assert r.status_code == 422 and big.status_code == 413
    out = capsys.readouterr()
    assert "audit_write_failed" in out.out
    assert SECRET.lower() not in (out.out + out.err).lower()
