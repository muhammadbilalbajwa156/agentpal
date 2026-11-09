"""External HTTP authorization matrix, ownership oracles and credential lifecycle."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

SPEC = Path(__file__).resolve().parents[3] / "docs/api/openapi.json"
AGENT_ONLY = {"createPaymentRequest", "executePaymentRequest", "requestOwnerDecision",
              "getMe", "getMyWallet", "listMyTransactions", "browseUrl",
              "createPayoutRequest", "executePayoutRequest"}
EITHER = {"listPaymentRequests", "getPaymentRequest", "reconcilePayoutRequest"}


def routes():
    return [(method.upper(), path, operation["operationId"])
            for path, methods in json.loads(SPEC.read_text())["paths"].items()
            if path.startswith("/v1") for method, operation in methods.items()]


def body_for(path):
    if path == "/v1/agents":
        return {"name": "T2 matrix"}
    if path == "/v1/wallets":
        return {"agent_id": "agt_" + "0" * 32, "currency": "USD",
                "monthly_budget": "5.00", "per_transaction_limit": "1.00"}
    if path.endswith("/credentials"):
        return {"overlap_seconds": 0}
    if path.endswith("/permissions"):
        return {"capabilities": ["wallet.read"]}
    if path.startswith("/v1/wallets/"):
        return {"monthly_budget": "5.00"}
    if path == "/v1/payment-requests":
        return {"amount": "0.01", "currency": "USD", "merchant_name": "Northwind",
                "merchant_domain": "example.com", "purpose": "T2 matrix"}
    if path == "/v1/payout-requests":
        return {"amount": "0.01", "currency": "USD", "receiver_email": "qa@example.com",
                "purpose": "T2 matrix"}
    if path == "/v1/setup/owner":
        return {"owner_id": "matrix", "display_name": "Matrix", "code": "not-the-setup-code"}
    if path.startswith("/v1/integrations/"):
        return {"fields": {"api_key": "synthetic-matrix-value"}}
    if path.endswith("/approve"):
        return {"terms_sha256": "0" * 64}
    if path.endswith("/browse") or path.endswith("/page-check"):
        return {"url": "https://127.0.0.1/"}
    if path.endswith("/refunds"):
        return {"amount": "0.01"}
    return {}


def absent_path(path):
    return path.replace("{agent_id}", "agt_" + "0" * 32).replace(
        "{wallet_id}", "wal_" + "0" * 32).replace(
        "{request_id}", "pay_" + "0" * 32).replace("{key_id}", "0" * 16)


def test_all_v1_routes_credential_matrix(world):
    """47 routes × 8 credential cases; known-resource isolation checked below."""
    evidence = []
    violations = []
    for method, path, operation in routes():
        for actor in (None, "wrong_prefix", "revoked", "a", "b", "owner_a", "owner_b",
                      "owner_no_mfa"):
            if actor == "owner_no_mfa":
                world.tokens[actor] = world.tokens["owner_a"]
            target = absent_path(path)
            if path == "/v1/paypal/reconciliation":
                target += "?start=2026-11-03T00:00:00Z&end=2026-11-03T00:01:00Z"
            response = world.call(method, target, actor, json=body_for(path),
                                  headers={"Idempotency-Key": "matrix-" + uuid4().hex})
            status = response.status_code
            evidence.append({"method": method, "path": path, "actor": actor,
                             "status": status, "error": response.json().get("error")})
            if path.endswith("/health"):
                valid = status == 200
            elif path == "/v1/setup/status":
                valid = status == 200
            elif path == "/v1/setup/owner":
                # Open route: a wrong setup code never creates an owner, whatever the caller sends.
                valid = status == 403 and response.json().get("error") in (
                    "setup_complete", "invalid_setup_code", "signup_disabled", "loopback_required")
            elif path.endswith("/webhooks"):
                valid = status == 400
            elif actor in (None, "wrong_prefix", "revoked"):
                valid = status == 401
            elif actor in ("a", "b") and operation not in AGENT_ONLY | EITHER:
                valid = status == 403
            elif actor.startswith("owner_") and operation in AGENT_ONLY:
                valid = status == 403
            elif "{" in path:
                valid = status == 404
            else:
                valid = status < 500 or (status == 503 and response.json().get("error") == "not_configured")
            if not valid:
                violations.append(evidence[-1])
    (world.directory / "matrix.json").write_text(json.dumps(evidence, indent=2))
    assert not violations, violations


@pytest.mark.parametrize("actor", ["b", "owner_b"])
def test_cross_tenant_payment_routes_are_indistinguishable_404(world, actor):
    payment = world.payment().json()
    request_id = payment["request_id"]
    paths = [("GET", ""), ("POST", "/execute"), ("POST", "/approve"),
             ("POST", "/reject"), ("POST", "/capture"), ("GET", "/reconciliation"),
             ("POST", "/reconciliation"), ("POST", "/market-check"),
             ("POST", "/page-check"), ("GET", "/refunds"), ("POST", "/refunds"),
             ("POST", "/refunds/reconciliation"), ("POST", "/request-decision")]
    for method, suffix in paths:
        args = {"terms_sha256": payment["terms_sha256"]} if suffix == "/approve" else {}
        known = world.call(method, "/v1/payment-requests/" + request_id + suffix, actor,
                           json=args, headers={"Idempotency-Key": "t2-isolation-key"})
        absent = world.call(method, "/v1/payment-requests/pay_" + "0" * 32 + suffix,
                            actor, json=args, headers={"Idempotency-Key": "t2-isolation-key"})
        assert (known.status_code, known.content) == (absent.status_code, absent.content), suffix
        assert known.status_code == (403 if (actor == "b" and suffix not in
               ("", "/execute", "/request-decision")) or
               (actor == "owner_b" and suffix in ("/execute", "/request-decision")) else 404)


def test_cross_tenant_agent_wallet_mutations(world):
    for method, template, body in [
        ("GET", "/agents/{id}", {}), ("GET", "/agents/{id}/permissions", {}),
        ("PUT", "/agents/{id}/permissions", {"capabilities": []}),
        ("POST", "/agents/{id}/credentials", {}),
        ("DELETE", "/agents/{id}/credentials/" + "0" * 16, {}),
        ("POST", "/agents/{id}/suspend", {}), ("POST", "/agents/{id}/reactivate", {}),
        ("GET", "/wallets/{id}", {}), ("PATCH", "/wallets/{id}", {"monthly_budget": "5.00"}),
        ("POST", "/wallets/{id}/freeze", {}), ("POST", "/wallets/{id}/unfreeze", {}),
        ("GET", "/wallets/{id}/transactions", {}),
    ]:
        is_wallet = "/wallets/" in template
        resource = world.wallets["a"] if is_wallet else world.agents["a"]
        absent = ("wal_" if is_wallet else "agt_") + "0" * 32
        first = world.call(method, "/v1" + template.format(id=resource), "owner_b", json=body)
        second = world.call(method, "/v1" + template.format(id=absent), "owner_b", json=body)
        assert (first.status_code, first.content) == (404, second.content)
        assert second.status_code == 404


def test_revocation_suspension_and_capability_removal(world):
    registration = world.register("lifecycle", "owner_a")
    agent_id = registration["agent"]["agent_id"]
    assert world.call("GET", "/v1/me", "lifecycle").status_code == 200
    assert world.call("POST", f"/v1/agents/{agent_id}/suspend", "owner_a").status_code == 200
    assert world.call("GET", "/v1/me", "lifecycle").status_code == 401
    world.call("POST", f"/v1/agents/{agent_id}/reactivate", "owner_a")
    world.call("PUT", f"/v1/agents/{agent_id}/permissions", "owner_a", json={"capabilities": []})
    assert world.payment("lifecycle").status_code == 403
    rotation = world.call("POST", f"/v1/agents/{agent_id}/credentials", "owner_a", json={}).json()
    assert world.call("GET", "/v1/me", "lifecycle").status_code == 401
    world.tokens["lifecycle"] = rotation["api_key"]
    assert world.call("GET", "/v1/me", "lifecycle").status_code == 200


def test_same_owner_sibling_agent_cannot_read_or_execute_another_request(world):
    world.register("sibling", "owner_a")
    payment = world.payment("sibling").json()
    path = "/v1/payment-requests/" + payment["request_id"]
    for method, suffix in (("GET", ""), ("POST", "/execute"), ("POST", "/request-decision")):
        response = world.call(method, path + suffix, "a")
        assert (response.status_code, response.json()["error"]) == (404, "not_found")
    listing = world.call("GET", "/v1/payment-requests", "a").json()
    assert payment["request_id"] not in json.dumps(listing)


def test_frozen_wallet_and_revoked_execute_grant_override_prior_approval(world):
    world.register("frozen", "owner_a")
    payment = world.payment("frozen").json()
    path = "/v1/payment-requests/" + payment["request_id"]
    response = world.call("POST", path + "/approve", "owner_a",
                          json={"terms_sha256": payment["terms_sha256"]})
    assert response.status_code == 200
    assert world.call("POST", "/v1/wallets/" + world.wallets["frozen"] + "/freeze",
                      "owner_a").status_code == 200
    assert world.call("POST", path + "/execute", "frozen").status_code == 409
    world.call("POST", "/v1/wallets/" + world.wallets["frozen"] + "/unfreeze", "owner_a")
    response = world.call("PUT", "/v1/agents/" + world.agents["frozen"] + "/permissions",
                          "owner_a", json={"capabilities": ["wallet.read", "payment.prepare"]})
    assert response.status_code == 200
    assert world.call("POST", path + "/execute", "frozen").status_code == 403
