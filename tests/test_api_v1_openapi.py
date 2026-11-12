"""The published OpenAPI is a deterministic consumer contract, including operation IDs."""

import json

from agentpal.platform.openapi import DEFAULT_PATH, build_document, render


OPERATIONS = {
    "POST /v1/wallets/freeze-all": "freezeAllWallets",
    "GET /v1/paypal/reconciliation": "reconcileWithPaypal",
    "POST /v1/payout-requests": "createPayoutRequest",
    "GET /v1/payout-requests": "listPayoutRequests",
    "GET /v1/payout-requests/{request_id}": "getPayoutRequest",
    "GET /v1/payout-requests/{request_id}/timeline": "getPayoutRequestTimeline",
    "GET /v1/setup/status": "getSetupStatus",
    "POST /v1/setup/owner": "setupOwner",
    "GET /v1/integrations": "listIntegrations",
    "PUT /v1/integrations/{provider}": "putIntegration",
    "DELETE /v1/integrations/{provider}": "deleteIntegration",
    "POST /v1/integrations/{provider}/verify": "verifyIntegration",
    "POST /v1/lab/agent": "createLabAgent",
    "POST /v1/lab/payment-requests": "createLabPayment",
    "POST /v1/lab/scenarios/{name}": "runLabScenario",
    "POST /v1/lab/payment-requests/{request_id}/execute": "executeLabPayment",
    "POST /v1/lab/errands": "createLabErrand",
    "POST /v1/payout-requests/{request_id}/approve": "approvePayoutRequest",
    "POST /v1/payout-requests/{request_id}/reject": "rejectPayoutRequest",
    "POST /v1/payout-requests/{request_id}/execute": "executePayoutRequest",
    "POST /v1/payout-requests/{request_id}/reconciliation": "reconcilePayoutRequest",
    "GET /v1/health": "v1Health",
    "POST /v1/agents": "registerAgent",
    "GET /v1/agents": "listAgents",
    "GET /v1/agents/{agent_id}": "getAgent",
    "POST /v1/agents/{agent_id}/credentials": "rotateAgentCredential",
    "DELETE /v1/agents/{agent_id}/credentials/{key_id}": "revokeAgentCredential",
    "POST /v1/agents/{agent_id}/suspend": "suspendAgent",
    "POST /v1/agents/{agent_id}/reactivate": "reactivateAgent",
    "GET /v1/agents/{agent_id}/permissions": "getAgentPermissions",
    "PUT /v1/agents/{agent_id}/permissions": "setAgentPermissions",
    "POST /v1/wallets": "createWallet",
    "GET /v1/wallets": "listWallets",
    "GET /v1/wallets/{wallet_id}": "getWallet",
    "PATCH /v1/wallets/{wallet_id}": "updateWallet",
    "POST /v1/wallets/{wallet_id}/freeze": "freezeWallet",
    "POST /v1/wallets/{wallet_id}/unfreeze": "unfreezeWallet",
    "GET /v1/wallets/{wallet_id}/transactions": "listWalletTransactions",
    "GET /v1/me": "getMe",
    "GET /v1/me/wallet": "getMyWallet",
    "GET /v1/me/wallet/transactions": "listMyTransactions",
    "POST /v1/payment-requests": "createPaymentRequest",
    "GET /v1/payment-requests": "listPaymentRequests",
    "GET /v1/payment-requests/{request_id}": "getPaymentRequest",
    "GET /v1/payment-requests/{request_id}/timeline": "getPaymentRequestTimeline",
    "POST /v1/payment-requests/{request_id}/request-decision": "requestOwnerDecision",
    "GET /v1/approvals": "listApprovals",
    "POST /v1/payment-requests/{request_id}/approve": "approvePaymentRequest",
    "POST /v1/payment-requests/{request_id}/reject": "rejectPaymentRequest",
    "POST /v1/payment-requests/{request_id}/counter": "counterPaymentRequest",
    "POST /v1/payment-requests/{request_id}/execute": "executePaymentRequest",
    "POST /v1/payment-requests/{request_id}/capture": "capturePaymentRequest",
    "POST /v1/payment-requests/{request_id}/reconciliation": "reconcilePaymentRequest",
    "POST /v1/payment-requests/{request_id}/market-check": "refreshMarketCheck",
    "GET /v1/payment-requests/{request_id}/reconciliation": "getReconciliation",
    "POST /v1/payment-requests/{request_id}/page-check": "checkMerchantPage",
    "POST /v1/research/browse": "browseUrl",
    "POST /v1/providers/paypal/webhooks": "receivePaypalWebhook",
    "POST /v1/payment-requests/{request_id}/refunds": "refundPayment",
    "GET /v1/payment-requests/{request_id}/refunds": "listPaymentRefunds",
    "POST /v1/payment-requests/{request_id}/refunds/reconciliation": "reconcilePaymentRefunds",
    "GET /v1/disputes": "listDisputes",
}


def test_openapi_has_no_drift_and_build_is_deterministic(monkeypatch):
    first = render()
    monkeypatch.setenv("AGENTPAL_API_TOKENS_FILE", "/missing-do-not-read.json")
    monkeypatch.setenv("AGENTPAL_ANALYZER_CHAIN", "does-not-exist")
    assert render() == first
    assert DEFAULT_PATH.read_text(encoding="utf-8") == first
    assert json.loads(first)["servers"][0]["url"] == "http://127.0.0.1:8000"


def test_operation_ids_are_stable_unique_and_auth_schemes_are_explicit():
    doc = build_document()
    actual = {f"{method.upper()} {path}": op["operationId"]
              for path, methods in doc["paths"].items() if path.startswith("/v1/")
              for method, op in methods.items()}
    assert actual == OPERATIONS
    ids = [op["operationId"] for methods in doc["paths"].values() for op in methods.values()]
    assert len(ids) == len(set(ids))
    for path, methods in doc["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for op in methods.values():
            if path in {"/v1/health", "/v1/providers/paypal/webhooks", "/v1/setup/status", "/v1/setup/owner"}:
                assert op["security"] == []
            else:
                assert op["security"]
    assert doc["components"]["schemas"]["PaymentCreate"]["properties"]["amount"]["type"] == "string"


def test_generator_response_annotations_are_idempotent_and_preserve_route_parameters():
    from copy import deepcopy

    from agentpal.platform.openapi import _document_platform_errors

    doc = build_document()
    before = deepcopy(doc)
    _document_platform_errors(doc)
    assert doc == before
    # No request schema or header patching is permitted here; A1 owns that source.
    for path in doc["paths"]:
        if path.startswith("/v1/"):
            for method in doc["paths"][path]:
                assert doc["paths"][path][method].get("parameters") == \
                    before["paths"][path][method].get("parameters")
