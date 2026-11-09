import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.config import Settings, load_policy
from agentpal.schemas import RiskAssessment
from agentpal.service import RiskService
from agentpal.store import AssessmentStore, redact

from conftest import VALID, FakeAnalyzer, analysis

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "requests"


@pytest.fixture
def service():
    return RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()])


@pytest.fixture
def client(service):
    app = create_app(service=service, settings=Settings(max_request_bytes=8192))
    with TestClient(app) as c:
        yield c


def test_health_and_model_info(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["payment_execution_enabled"] is False
    assert h["active_backend"] == "heuristic" and h["model_backend_available"] is False
    info = client.get("/model-info").json()
    assert info["active_backend"] == "heuristic"
    assert info["backends"][0]["is_fallback"] is True


def test_assess_returns_valid_schema_and_persists(client):
    r = client.post("/assess", json=VALID)
    assert r.status_code == 200
    body = RiskAssessment.model_validate(r.json())
    assert body.recommendation == "allow_for_review"
    assert body.payment_executed is False
    got = client.get(f"/assessments/{VALID['request_id']}")
    assert got.status_code == 200 and got.json()["risk_score"] == body.risk_score


def test_replayed_request_id_is_blocked_and_original_kept(client):
    first = client.post("/assess", json=VALID).json()
    replay = client.post("/assess", json=VALID).json()
    assert replay["recommendation"] == "block"
    assert "DUPLICATE_REQUEST_ID" in replay["hard_violations"]
    stored = client.get(f"/assessments/{VALID['request_id']}").json()
    assert stored["recommendation"] == first["recommendation"]


def test_duplicate_intent_across_requests(client):
    client.post("/assess", json=VALID)
    second = client.post("/assess", json={**VALID, "request_id": "req_demo_002"}).json()
    assert "DUPLICATE_PAYMENT_INTENT" in {s["code"] for s in second["signals"]}
    assert second["recommendation"] != "allow_for_review"


@pytest.mark.parametrize("bad", [
    {**VALID, "amount": -5}, {**VALID, "amount": 0}, {**VALID, "amount": "lots"},
    {**VALID, "currency": "ZZZ"}, {k: v for k, v in VALID.items() if k != "amount"},
    {**VALID, "unexpected": 1},
])
def test_invalid_requests_rejected_safely(client, bad):
    r = client.post("/assess", json=bad)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"
    assert all(set(d) == {"loc", "msg", "type"} for d in r.json()["details"])  # no raw input echoed


def test_malformed_json_and_oversized_body(client):
    r = client.post("/assess", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = client.post("/assess", content=b"x" * 10000, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_get_unknown_and_invalid_ids(client):
    assert client.get("/assessments/req_unknown_1").status_code == 404
    assert client.get("/assessments/bad%20id!").status_code == 422


def test_internal_errors_are_not_leaked(service):
    def boom(*a, **k):
        raise RuntimeError("secret detail sk_live_abcdefghijklmnop")

    service.assess = boom
    app = create_app(service=service, settings=Settings())
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/assess", json=VALID)
    assert r.status_code == 500 and r.json() == {"error": "internal_error"}


def test_example_files(client):
    results = {}
    for path in sorted(EXAMPLES.glob("*.json")):
        r = client.post("/assess", json=json.loads(path.read_text()))
        results[path.stem] = r
    assert results["90_invalid_negative_amount"].status_code == 422
    assert results["01_legit_hosting"].json()["recommendation"] == "allow_for_review"
    assert results["02_legit_with_invoice"].json()["recommendation"] == "allow_for_review"
    for name, r in results.items():
        if name.startswith("1"):
            assert r.status_code == 200, name
            assert r.json()["recommendation"] in ("require_review", "block"), name


# ------------------------------------------------------------------ audit

def test_audit_record_created(service):
    from agentpal.schemas import PaymentRequest

    service.chain = [FakeAnalyzer(analysis(score=12))]
    req = PaymentRequest.model_validate(VALID)
    a = service.assess(req)
    rows = service.store.audit_records(VALID["request_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "assessed"
    assert row["model_backend"] == "fake" and row["model_name"] == "fake-model"
    assert row["rules_version"] == service.policy.rules_version
    assert row["risk_score"] == a.risk_score and row["recommendation"] == a.recommendation
    assert "MISSING_EVIDENCE" in row["signal_codes"]
    service.assess(req)  # replay is audited too
    assert [r["status"] for r in service.store.audit_records(VALID["request_id"])] == [
        "assessed", "replay_rejected"]


def test_audit_does_not_store_secrets(service):
    from agentpal.schemas import PaymentRequest

    secret = "api_key=sk_live_ABCDEF1234567890XYZ and Bearer abcdefghijklmnopqrstuv"
    req = PaymentRequest.model_validate({**VALID, "business_justification": secret})
    service.assess(req)
    dumped = json.dumps(service.store.audit_records()) + \
        service.store.get_assessment(VALID["request_id"]).model_dump_json()
    assert "sk_live_ABCDEF1234567890XYZ" not in dumped
    assert "abcdefghijklmnopqrstuv" not in dumped
    assert redact("card 4111111111111111") == "card [REDACTED]"


def test_flagged_request_does_not_poison_merchant_history(client):
    """A lookalike-domain request must not make the genuine domain look 'changed'."""
    # listed agents: an unlisted agent would get the demo default budget, which always
    # requires review (final owner decision 4) and would hide what this test checks
    bad = {**VALID, "request_id": "req_bad_001", "agent_id": "agent_demo_002",
           "merchant_domain": "examp1e-billing.com"}
    assert client.post("/assess", json=bad).json()["recommendation"] != "allow_for_review"
    good = client.post("/assess", json={**VALID, "agent_id": "agent_demo_001"}).json()
    assert "MERCHANT_DOMAIN_CHANGED" not in {s["code"] for s in good["signals"]}
    assert good["recommendation"] == "allow_for_review"


def test_missing_information_alone_is_not_high_risk(client):
    sparse = {"request_id": "req_sparse_1", "agent_id": "agent_sparse_1", "amount": 20,
              "currency": "USD", "merchant_name": "Example Hosting", "purpose": "Hosting",
              "recurring": False}
    body = client.post("/assess", json=sparse).json()
    assert body["recommendation"] == "require_review"
    assert body["risk_category"] == "medium"
    assert all(s["kind"] == "uncertainty" for s in body["signals"]
               if s["code"] != "UNAPPROVED_MERCHANT")
