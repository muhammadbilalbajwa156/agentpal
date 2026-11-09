"""Owner decision 10: the API stays bound to localhost until authentication,
authorization, rate limiting and deployment security exist and are tested."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentpal.__main__ import main as cli
from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.config import Settings, load_policy
from agentpal.service import RiskService
from agentpal.store import AssessmentStore

from conftest import VALID


def service():
    return RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()])


@pytest.mark.parametrize("peer,status", [
    (("203.0.113.5", 40000), 403), (("10.0.0.7", 40000), 403), (("::ffff:198.51.100.1", 1), 403),
    (("127.0.0.1", 40000), 200), (("::1", 40000), 200), (("::ffff:127.0.0.1", 40001), 200),
])
def test_only_loopback_clients_are_served(peer, status):
    s = service()
    with TestClient(create_app(service=s, settings=Settings()), client=peer) as c:
        assert c.get("/health").status_code == status
        r = c.post("/assess", json={**VALID, "request_id": f"req_peer_{peer[1]}"})
        assert r.status_code == (200 if status == 200 else 403)
    if status == 403:
        assert any(row["status"] == "rejected:non_loopback_client"
                   for row in s.store.audit_records())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "203.0.113.9", "example.com"])
def test_serve_refuses_non_loopback_bind(host, monkeypatch):
    called = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append(k))
    with pytest.raises(SystemExit):
        cli(["serve", "--host", host])
    assert called == []


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_serve_allows_loopback_bind(host, monkeypatch):
    called = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append(k))
    cli(["serve", "--host", host])
    assert called and called[0]["host"] == host
    assert called[0]["proxy_headers"] is False  # forwarded headers never set the client


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_forwarded_headers_cannot_make_a_remote_client_local(header):
    value = "for=127.0.0.1" if header == "Forwarded" else "127.0.0.1"
    s = service()
    with TestClient(create_app(service=s, settings=Settings()),
                    client=("203.0.113.5", 40000)) as c:
        assert c.get("/health", headers={header: value}).status_code == 403
        assert c.post("/assess", json={**VALID, "request_id": "req_fwd_1"},
                      headers={header: value}).status_code == 403


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded"])
def test_forwarded_headers_do_not_change_a_local_clients_identity(header):
    value = "for=203.0.113.5" if header == "Forwarded" else "203.0.113.5"
    with TestClient(create_app(service=service(), settings=Settings()),
                    client=("127.0.0.1", 40000)) as c:
        assert c.get("/health", headers={header: value}).status_code == 200
