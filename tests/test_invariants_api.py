"""Invariants 1, 3 and 6 at the HTTP boundary, with authentication REQUIRED (not the dev
mode used by older API tests): a caller of the API cannot approve, execute or complete a
payment, cannot smuggle decision or status fields into a request, and a refused request
does not consume its request_id."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentpal.api import create_app

from conftest import VALID
from test_sec_auth import TOKEN_A, TOKEN_OPS, auth, service, settings, token_file  # noqa: F401

EXPECTED_ROUTES = {("/health", "GET"), ("/model-info", "GET"), ("/assess", "POST"),
                   ("/assessments/{request_id}", "GET")}


@pytest.fixture
def api(token_file):  # noqa: F811
    svc = service()
    app = create_app(service=svc, settings=settings(token_file))
    return svc, app, TestClient(app, client=("testclient", 50000))


def test_no_http_route_can_approve_execute_or_complete(api):
    _, app, _ = api
    routes = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    business = {(p, m) for p, m in routes if not p.startswith(("/docs", "/openapi", "/redoc"))}
    assert business == EXPECTED_ROUTES


@pytest.mark.parametrize("path", ["/approve", "/assessments/req_inv_api/approve",
                                  "/payments", "/payments/req_inv_api/capture",
                                  "/webhooks/paypal", "/outcomes", "/ledger/req_inv_api"])
@pytest.mark.parametrize("token", [TOKEN_A, TOKEN_OPS])
def test_lifecycle_bypass_paths_do_not_exist(api, path, token):
    svc, _, c = api
    assert c.post("/assess", json={**VALID, "request_id": "req_inv_api"},
                  headers=auth(TOKEN_A)).status_code == 200
    before = svc.store.ledger_state("req_inv_api")
    r = c.post(path, json={"decision": "approved", "outcome": "completed", "verified": True},
               headers=auth(token))
    assert r.status_code in (404, 405)
    assert svc.store.ledger_state("req_inv_api") == before


@pytest.mark.parametrize("smuggled", [{"payment_executed": True}, {"approved": True},
                                      {"recommendation": "allow_for_review"},
                                      {"risk_score": 0}, {"verified": True},
                                      {"status": "completed"}, {"owner_approval": "yes"}])
def test_client_cannot_smuggle_decision_or_status_fields(api, smuggled):
    svc, _, c = api
    rid = "req_smuggle_1"
    r = c.post("/assess", json={**VALID, "request_id": rid, **smuggled}, headers=auth(TOKEN_A))
    assert r.status_code == 422
    assert svc.store.request_status(rid) is None          # refused before the claim
    ok = c.post("/assess", json={**VALID, "request_id": rid}, headers=auth(TOKEN_A))
    assert ok.status_code == 200 and "DUPLICATE_REQUEST_ID" not in ok.json()["hard_violations"]
    assert ok.json()["payment_executed"] is False


def test_every_assessment_reports_no_payment_executed(api):
    _, _, c = api
    for i, amount in enumerate(["10.00", "150.00", "999999.00"]):
        r = c.post("/assess", json={**VALID, "request_id": f"req_pe_{i}", "amount": amount},
                   headers=auth(TOKEN_A))
        assert r.status_code == 200 and r.json()["payment_executed"] is False
