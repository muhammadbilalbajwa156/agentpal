"""Published contract against real /v1 responses (route fixes applied by the lead)."""

import pytest

from agentpal.platform.openapi import build_document
from test_platform_fixtures import OWNER_A, build_world



def validator(schema, document):
    jsonschema = pytest.importorskip("jsonschema", reason="lead adds jsonschema==4.26.0")
    return jsonschema.Draft202012Validator({**schema, "components": document["components"]})


def test_validation_envelopes_match_real_responses():
    doc = build_document()
    schema = doc["paths"]["/v1/payment-requests"]["post"]["responses"]["422"][
        "content"]["application/json"]["schema"]
    world = build_world()
    _, key, _ = world.funded_agent()
    response = world.pay(key, amount=1.25)
    assert response.status_code == 422
    validator(schema, doc).validate(response.json())
    assert response.json()["error"] == "invalid_request"
    assert "details" in response.json()
    validator(schema, doc).validate({"error": "invalid_request"})
    assert not validator(schema, doc).is_valid({"detail": []})
    assert not validator(schema, doc).is_valid({"error": "invalid_request", "input": "secret"})


def test_common_errors_and_response_headers():
    doc = build_document()
    for path, methods in doc["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for operation in methods.values():
            responses = operation["responses"]
            for code in ("400", "403", "413", "429", "500"):
                assert responses[code]["content"]["application/json"]["schema"]
            assert "Retry-After" in responses["429"]["headers"]
            if operation.get("security"):
                assert "WWW-Authenticate" in responses["401"]["headers"]
    replay = doc["paths"]["/v1/payment-requests"]["post"]["responses"]["200"]
    assert replay["headers"]["Idempotent-Replayed"]["schema"]["enum"] == ["true"]


def test_required_nonnullable_idempotency_header():
    doc = build_document()
    parameters = doc["paths"]["/v1/payment-requests"]["post"]["parameters"]
    headers = [p for p in parameters if p["name"] == "Idempotency-Key"]
    assert len(headers) == 1
    assert headers[0]["required"] is True
    assert headers[0]["schema"]["type"] == "string"
    world = build_world()
    _, key, _ = world.funded_agent()
    body = {"amount": "1.00", "currency": "USD", "merchant_name": "m", "purpose": "p"}
    response = world.client.post("/v1/payment-requests", json=body, headers=world.h(key))
    assert response.status_code == 400
    assert response.json() == {"error": "idempotency_key_required"}
    assert world.pay(key, idem="short").status_code == 422


@pytest.mark.parametrize("amount", [1, 2, 49])
def test_integer_money_is_refused_without_a_reservation(amount):
    world = build_world()
    _, key, _ = world.funded_agent()
    assert world.pay(key, amount=amount).status_code == 422
    assert world.wallet_store.query("SELECT * FROM wallet_payment_requests") == []


def test_wallet_patch_schema_requires_a_nonnull_change():
    doc = build_document()
    schema = doc["components"]["schemas"]["WalletUpdate"]
    check = validator(schema, doc)
    assert not check.is_valid({})
    assert not check.is_valid({"monthly_budget": None})
    check.validate({"monthly_budget": "100.00"})
    world = build_world()
    aid, _, wallet = world.funded_agent()
    response = world.client.patch("/v1/wallets/" + wallet["wallet_id"], json={},
                                  headers=world.h(OWNER_A))
    assert response.status_code == 422
    assert world.platform.registry.get_agent(aid) is not None


def test_metadata_schema_matches_runtime_constraints():
    doc = build_document()
    check = validator({"$ref": "#/components/schemas/AgentCreate"}, doc)
    assert not check.is_valid({"name": "bot", "metadata": {"BAD KEY": "value"}})
    assert not check.is_valid({"name": "bot", "metadata": {"good": "v" * 201}})
    check.validate({"name": "bot", "metadata": {"good_key": "v" * 200}})


def test_webhook_documents_raw_body_and_all_signature_headers():
    operation = build_document()["paths"]["/v1/providers/paypal/webhooks"]["post"]
    assert operation["security"] == []  # signature verification, never bearer permission
    assert operation["requestBody"]["required"] is True
    assert "application/json" in operation["requestBody"]["content"]
    headers = {p["name"].lower(): p for p in operation["parameters"]}
    for name in ("paypal-transmission-id", "paypal-transmission-time",
                 "paypal-transmission-sig", "paypal-cert-url", "paypal-auth-algo"):
        assert headers[name]["required"] is True
        assert headers[name]["schema"]["type"] == "string"
