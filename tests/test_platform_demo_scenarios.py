"""Proof for the judge runbook, through public Test Lab endpoints."""
from cryptography.fernet import Fernet

from test_platform_workspace import bearer, owner, payment, world


def test_lab_over_limit_and_split_purchase_are_blocked_before_approval(tmp_path):
    with world(tmp_path, {"AGENTPAL_MASTER_KEY": Fernet.generate_key().decode()}) as (c, d):
        h = bearer(owner(d))
        lab = c.post("/v1/lab/agent", headers=h).json()
        wallet = c.get("/v1/wallets/" + lab["wallet_id"], headers=h).json()
        assert wallet["limits"]["per_transaction_limit"] == "25.00"
        over = c.post("/v1/lab/payment-requests", headers=h, json=payment("30.00"))
        assert over.status_code == 201, over.text
        assert over.json()["recommendation"] == "block"
        assert "AMOUNT_ABOVE_HARD_LIMIT" in {s["code"] for s in over.json()["risk_signals"]}
        # Different merchant from the refused $30 attempt; only the two $15 requests aggregate.
        parts = []
        for purpose in ("Paper part one", "Paper part two"):
            r = c.post("/v1/lab/payment-requests", headers=h,
                json={**payment("15.00"), "merchant_name": "Paper Supply",
                      "merchant_domain": "paper.example", "purpose": purpose})
            assert r.status_code == 201, r.text
            parts.append(r.json())
        assert parts[0]["status"] == "awaiting_approval"
        assert parts[1]["recommendation"] == "block"
        assert "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT" in {s["code"] for s in parts[1]["risk_signals"]}
        pending = c.get("/v1/approvals", headers=h).json()
        assert parts[1]["request_id"] not in str(pending)
        assert over.json()["request_id"] not in str(pending)
        assert c.post("/v1/lab/payment-requests/" + parts[1]["request_id"] + "/execute", headers=h).status_code == 409


def test_old_terms_digest_cannot_approve_changed_purchase(tmp_path):
    with world(tmp_path, {"AGENTPAL_MASTER_KEY": Fernet.generate_key().decode()}) as (c, d):
        h = bearer(owner(d))
        old = c.post("/v1/lab/payment-requests", headers=h, json=payment()).json()
        assert c.post("/v1/payment-requests/" + old["request_id"] + "/approve", headers=h,
                      json={"terms_sha256": old["terms_sha256"]}).status_code == 200
        changed = c.post("/v1/lab/payment-requests", headers=h, json=payment("2.00")).json()
        assert old["terms_sha256"] != changed["terms_sha256"]
        rid = changed["request_id"]
        response = c.post(f"/v1/payment-requests/{rid}/approve", headers=h,
                          json={"terms_sha256": old["terms_sha256"]})
        assert response.status_code == 409
        assert response.json()["error"] == "terms_mismatch"
        assert c.post(f"/v1/lab/payment-requests/{rid}/execute", headers=h).status_code == 409
