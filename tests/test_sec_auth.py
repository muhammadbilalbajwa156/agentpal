"""Security audit (helper 1): API authentication, authorization, rate limiting, Host
allow-list and consistent errors (SEC-01..SEC-04, SEC-10, SEC-12).

The HTTP-level tests were run against commit 77e08b2 (no authentication) before the fix
and failed there. New modules are imported inside the tests that need them for that
reason.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.config import Settings, load_policy
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy
from agentpal.store import AssessmentStore

from conftest import VALID

TOKEN_A = "apt_agent_a_" + "a" * 40
TOKEN_B = "apt_agent_b_" + "b" * 40
TOKEN_OPS = "apt_operator_" + "c" * 40
LIMITS = {"USD": {"monthly_budget": "1000.00", "rolling_window_limit": "1000.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "api_tokens.json"
    p.write_text(json.dumps({"tokens": [
        {"id": "key-a", "role": "agent", "agent_id": "agent_demo_001", "sha256": sha(TOKEN_A)},
        {"id": "key-b", "role": "agent", "agent_id": "agent_demo_002", "sha256": sha(TOKEN_B)},
        {"id": "ops", "role": "operator", "sha256": sha(TOKEN_OPS)}]}))
    return p


def service():
    sp = SpendPolicy.model_validate({
        "policy_version": "t",
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": LIMITS},
                   "agent_demo_002": {"owner_id": "o", "currencies": LIMITS}}})
    return RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       spend_policy=sp)


def settings(token_file, **kw):
    base = dict(auth_mode="required", api_tokens_file=str(token_file),
                allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],
                rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=1000)
    base.update(kw)
    return Settings(**base)


def client(token_file, svc=None, peer=("testclient", 50000), base_url="http://testserver",
           **kw):
    svc = svc or service()
    return svc, TestClient(create_app(service=svc, settings=settings(token_file, **kw)),
                           client=peer, base_url=base_url)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def body(rid="req_sec_001", agent="agent_demo_001"):
    return {**VALID, "request_id": rid, "agent_id": agent}


def rejection_reasons(svc):
    return [r["status"] for r in svc.store.audit_records() if r["status"].startswith("rejected:")]


# -------------------------------------------------------------- SEC-01 authentication
def test_authentication_is_required_by_default(monkeypatch):
    monkeypatch.delenv("AGENTPAL_AUTH_MODE", raising=False)
    assert Settings().auth_mode == "required"
    assert Settings.from_env().auth_mode == "required"


@pytest.mark.parametrize("path,method", [("/assess", "post"), ("/assessments/req_x_001", "get"),
                                         ("/model-info", "get")])
@pytest.mark.parametrize("headers,error", [
    ({}, "authentication_required"),
    ({"Authorization": "Bearer " + "z" * 40}, "invalid_token"),
    ({"Authorization": "Basic " + TOKEN_A}, "authentication_required"),
    ({"Authorization": "Bearer short"}, "invalid_token"),
])
def test_missing_or_invalid_token_is_refused(token_file, path, method, headers, error):
    svc, c = client(token_file)
    with c:
        kwargs = {"json": body()} if method == "post" else {}
        r = getattr(c, method)(path, headers=headers, **kwargs)
    assert r.status_code == 401 and r.json() == {"error": error}
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert svc.store.request_status("req_sec_001") is None  # nothing was claimed
    records = json.dumps(svc.store.audit_records())
    assert f"rejected:{error}" in records
    assert TOKEN_A not in records and "z" * 40 not in records  # no token in the audit


def test_health_stays_open_for_liveness(token_file):
    _, c = client(token_file)
    with c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json()["authentication"] == "required"


def test_two_authorization_headers_are_refused(token_file):
    _, c = client(token_file)
    with c:
        r = c.get("/model-info", headers=[("Authorization", f"Bearer {TOKEN_A}"),
                                          ("Authorization", f"Bearer {TOKEN_B}")])
    assert r.status_code == 401


def test_token_file_holds_only_hashes_and_lookup_compares_every_entry(token_file):
    from agentpal.auth import TokenRegistry

    text = token_file.read_text()
    assert TOKEN_A not in text and sha(TOKEN_A) in text
    reg = TokenRegistry.load(token_file)
    assert reg.authenticate(TOKEN_A).agent_id == "agent_demo_001"
    assert reg.authenticate(TOKEN_OPS).role == "operator"
    for bad in (None, "", TOKEN_A + "x", TOKEN_A[:-1], " " + TOKEN_A, sha(TOKEN_A),
                "é" * 30):
        assert reg.authenticate(bad) is None


@pytest.mark.parametrize("entry", [
    {"id": "x", "role": "agent", "sha256": "0" * 64},                       # no agent
    {"id": "x", "role": "operator", "agent_id": "agent_demo_001", "sha256": "0" * 64},
    {"id": "x", "role": "agent", "agent_id": "agent_demo_001", "sha256": "plaintext"},
    {"id": "x", "role": "agent", "agent_id": "agent_demo_001", "token": "plain",
     "sha256": "0" * 64},
])
def test_token_file_validation(entry):
    from agentpal.auth import TokenRegistry

    with pytest.raises(ValueError):
        TokenRegistry.model_validate({"tokens": [entry]})


def test_duplicate_hashes_are_refused():
    from agentpal.auth import TokenRegistry

    e = {"role": "operator", "sha256": "1" * 64}
    with pytest.raises(ValueError):
        TokenRegistry.model_validate({"tokens": [{**e, "id": "a"}, {**e, "id": "b"}]})


# --------------------------------------------------------------- SEC-02 agent binding
def test_agent_token_assesses_only_its_own_agent(token_file):
    svc, c = client(token_file)
    with c:
        ok = c.post("/assess", json=body("req_own_001"), headers=auth(TOKEN_A))
        bad = c.post("/assess", json=body("req_other_001", agent="agent_demo_002"),
                     headers=auth(TOKEN_A))
        assert ok.status_code == 200
        assert bad.status_code == 403 and bad.json() == {"error": "agent_mismatch"}
        # the forged request was refused BEFORE the claim: the id is not burnt, so the real
        # agent can still use it (no request_id squatting through a mismatched body)
        assert svc.store.request_status("req_other_001") is None
        real = c.post("/assess", json=body("req_other_001", agent="agent_demo_002"),
                      headers=auth(TOKEN_B))
    assert real.status_code == 200
    assert "DUPLICATE_REQUEST_ID" not in real.json()["hard_violations"]
    assert "rejected:agent_mismatch" in rejection_reasons(svc)


def test_operator_token_cannot_submit_assessments(token_file):
    svc, c = client(token_file)
    with c:
        r = c.post("/assess", json=body("req_ops_001"), headers=auth(TOKEN_OPS))
    assert r.status_code == 403 and r.json() == {"error": "agent_token_required"}
    assert svc.store.request_status("req_ops_001") is None


# ---------------------------------------------------------------------- SEC-01 BOLA
def test_assessment_is_readable_only_by_its_agent_or_an_operator(token_file):
    svc, c = client(token_file)
    with c:
        assert c.post("/assess", json=body("req_bola_001"), headers=auth(TOKEN_A)).status_code == 200
        own = c.get("/assessments/req_bola_001", headers=auth(TOKEN_A))
        ops = c.get("/assessments/req_bola_001", headers=auth(TOKEN_OPS))
        other = c.get("/assessments/req_bola_001", headers=auth(TOKEN_B))
        unknown = c.get("/assessments/req_never_001", headers=auth(TOKEN_B))
    assert own.status_code == 200 and own.json()["agent_id"] == "agent_demo_001"
    assert ops.status_code == 200
    # another agent cannot tell "exists but not yours" from "does not exist"
    assert other.status_code == unknown.status_code == 404
    assert other.json() == unknown.json() == {"error": "not_found"}
    assert "rejected:cross_agent_read" in rejection_reasons(svc)


# -------------------------------------------------------------- SEC-03 rate limiting
def test_per_token_rate_limit_returns_429_and_is_audited_once(token_file):
    svc, c = client(token_file, rate_limit_per_token_per_minute=3)
    with c:
        codes = [c.get("/model-info", headers=auth(TOKEN_A)).status_code for _ in range(8)]
        limited = c.get("/model-info", headers=auth(TOKEN_A))
        other_token = c.get("/model-info", headers=auth(TOKEN_B))
    assert codes == [200, 200, 200] + [429] * 5
    assert limited.json() == {"error": "rate_limited"}
    assert int(limited.headers["retry-after"]) >= 1
    assert other_token.status_code == 200  # limits are per token
    # a burst of rejections produces one audit record per key and window
    assert rejection_reasons(svc).count("rejected:rate_limited") == 1


def test_per_peer_rate_limit_applies_to_every_path_and_without_a_token(token_file):
    _, c = client(token_file, rate_limit_per_peer_per_minute=2)
    with c:
        codes = [c.get("/health").status_code for _ in range(2)]
        assert c.get("/health").status_code == 429
        assert c.post("/assess", json=body(), headers=auth(TOKEN_A)).status_code == 429
    assert codes == [200, 200]


def test_sliding_window_recovers_after_the_window():
    from agentpal.ratelimit import SlidingWindowLimiter

    t = {"now": 0.0}
    lim = SlidingWindowLimiter(2, 60.0, clock=lambda: t["now"])
    assert lim.hit("k") == (True, 0) and lim.hit("k") == (True, 0)
    allowed, retry = lim.hit("k")
    assert not allowed and retry == 60
    t["now"] = 59.0
    assert lim.hit("k")[0] is False   # rejected hits are not counted ...
    t["now"] = 60.0
    assert lim.hit("k") == (True, 0)  # ... so the window frees up on time


def test_limiter_memory_is_bounded():
    from agentpal.ratelimit import SlidingWindowLimiter

    lim = SlidingWindowLimiter(1, 60.0, clock=lambda: 0.0, max_keys=50)
    for i in range(500):
        lim.hit(f"k{i}")
    assert len(lim._hits) <= 50


# ------------------------------------------------- SEC-10 Host allow-list (DNS rebinding)
@pytest.mark.parametrize("host,status", [("127.0.0.1:8000", 200), ("localhost", 200),
                                         ("[::1]:8000", 200), ("evil.example", 400),
                                         ("127.0.0.1.nip.io", 400)])
def test_host_header_allow_list(token_file, host, status):
    svc, c = client(token_file, peer=("127.0.0.1", 50000),
                    allowed_hosts=["127.0.0.1", "localhost", "::1"])
    with c:
        r = c.get("/health", headers={"Host": host})
    assert r.status_code == status
    if status == 400:
        assert r.json() == {"error": "forbidden_host"}
        assert "rejected:forbidden_host" in rejection_reasons(svc)


def test_loopback_restriction_still_applies_before_authentication(token_file):
    _, c = client(token_file, peer=("203.0.113.7", 50000))
    with c:
        r = c.get("/model-info", headers=auth(TOKEN_OPS))
    assert r.status_code == 403 and r.json() == {"error": "forbidden_non_loopback_client"}


# ------------------------------------------------------------ dev mode is loud
def test_dev_mode_disables_auth_loudly(token_file, capsys):
    app = create_app(service=service(), settings=settings(
        token_file, auth_mode="disabled_insecure_dev"))
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    assert any(e["level"] == "WARNING" and "AUTHENTICATION AND AUTHORIZATION ARE DISABLED"
               in e.get("warning", "") for e in logged)
    with TestClient(app) as c:
        r = c.post("/assess", json=body())
        h = c.get("/health").json()
    assert r.status_code == 200 and r.headers["x-agentpal-auth"] == "disabled-insecure-dev"
    assert h["authentication"] == "disabled_insecure_dev"


# --------------------------------------------------- SEC-04 production configuration
@pytest.mark.parametrize("override,needle", [
    ({"auth_mode": "disabled_insecure_dev"}, "refused in production"),
    ({"api_tokens_file": None}, "AGENTPAL_API_TOKENS_FILE"),
    ({"require_model_integrity": False}, "model integrity"),
    ({"require_read_only_model_storage": False}, "model integrity"),
    ({"db_path": ":memory:"}, "in-memory"),
    ({"allowed_hosts": ["127.0.0.1", "agentpal.example.com"]}, "loopback Host"),
])
def test_production_refuses_unsafe_settings(token_file, override, needle):
    from agentpal.config import UnsafeConfiguration

    s = settings(token_file, environment="production", db_path="prod.db",
                 allowed_hosts=["127.0.0.1"], **{})
    s = s.model_copy(update=override)
    with pytest.raises(UnsafeConfiguration, match=needle):
        s.check_runtime(spend_policy_configuration="operator")


def test_production_refuses_a_demo_spend_policy(token_file):
    from agentpal.config import UnsafeConfiguration

    s = settings(token_file, environment="production", db_path="prod.db",
                 allowed_hosts=["127.0.0.1"])
    assert s.check_runtime(spend_policy_configuration="operator") == []
    with pytest.raises(UnsafeConfiguration, match="demo"):
        s.check_runtime(spend_policy_configuration="demo")
    with pytest.raises(UnsafeConfiguration):  # the shipped spend_policy.json is demo
        create_app(settings=s)


def test_disabled_auth_is_refused_for_a_non_loopback_bind(token_file):
    from agentpal.config import UnsafeConfiguration

    s = settings(token_file, auth_mode="disabled_insecure_dev")
    assert s.check_runtime(host="127.0.0.1")
    with pytest.raises(UnsafeConfiguration, match="loopback"):
        s.check_runtime(host="0.0.0.0")


def test_serve_refuses_production_with_auth_disabled(monkeypatch):
    from agentpal import __main__ as cli
    from agentpal.config import get_settings

    monkeypatch.setenv("AGENTPAL_ENV", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="refusing to start"):
            cli.main(["serve", "--host", "127.0.0.1"])
    finally:
        get_settings.cache_clear()


def test_production_hides_the_openapi_docs(token_file, tmp_path):
    s = settings(token_file, environment="production", db_path=str(tmp_path / "p.db"),
                 allowed_hosts=["127.0.0.1"])
    with TestClient(create_app(service=service(), settings=s)) as c:
        assert c.get("/docs").status_code == 401  # not even an open path
        assert c.get("/docs", headers=auth(TOKEN_OPS)).status_code == 404
        assert c.get("/openapi.json", headers=auth(TOKEN_OPS)).status_code == 404


# -------------------------------------------------------- SEC-12 consistent errors
def test_errors_are_consistent_json_and_do_not_reflect_input(token_file):
    _, c = client(token_file)
    with c:
        missing = c.get("/nope", headers=auth(TOKEN_A))
        method = c.delete("/assess", headers=auth(TOKEN_A))
        extra = c.post("/assess", headers=auth(TOKEN_A),
                       json={**body(), "<script>alert(1)</script>": 1})
        many = c.post("/assess", headers=auth(TOKEN_A),
                      json={**body(), **{f"x{i}": i for i in range(200)}})
    assert missing.status_code == 404 and missing.json() == {"error": "not_found"}
    assert method.status_code == 405 and method.json() == {"error": "method_not_allowed"}
    assert extra.status_code == 422 and "<script>" not in extra.text
    assert extra.json()["error"] == "invalid_request"
    assert len(many.json()["details"]) <= 20


# --------------------------------------------------------------------- token CLI
def test_new_token_cli_prints_the_token_once_and_stores_only_its_hash(tmp_path, capsys):
    from agentpal import __main__ as cli
    from agentpal.auth import TokenRegistry

    f = tmp_path / "tokens.json"
    cli.main(["new-token", "--agent", "agent_demo_001", "--id", "k1", "--append", str(f)])
    err = capsys.readouterr().err
    token = err.strip().rsplit(" ", 1)[-1]
    assert token.startswith("apt_") and token not in f.read_text()
    assert TokenRegistry.load(f).authenticate(token).agent_id == "agent_demo_001"
    cli.main(["new-token", "--operator", "--id", "ops1", "--append", str(f)])
    assert len(json.loads(f.read_text())["tokens"]) == 2
