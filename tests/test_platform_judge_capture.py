"""A capture POST is never proof of an authenticated final PayPal GET."""
import httpx
import pytest

from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
from test_paypal_refunds import RefundPayPal
from test_platform_workspace import bearer, owner, payment, world


@pytest.mark.parametrize("mode", ["ok", "down", "inexact", "crash"])
def test_capture_attempts_final_authenticated_read_best_effort(tmp_path, mode):
    backend = RefundPayPal()
    reads = []

    def handle(req):
        if req.method == "GET" and "/checkout/orders/" in req.url.path and backend.status == "COMPLETED":
            reads.append(req)
            if mode == "down":
                return httpx.Response(503, json={})
            if mode == "crash":
                raise RuntimeError("synthetic read crash")
            if mode == "inexact":
                result = backend.order()
                result["purchase_units"][0]["amount"]["value"] = "0.99"
                return httpx.Response(200, json=result)
        return backend.handler(req)

    with world(tmp_path) as (c, d):
        d.provider = PayPalSandboxProvider(PayPalSandboxConfig(client_id="synthetic-client", client_secret="synthetic-private"),
            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        d.lifecycle.provider = d.provider
        h = bearer(owner(d))
        r = c.post("/v1/lab/payment-requests", headers=h, json=payment())
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["provider_mode"] == "paypal_sandbox"
        path = "/v1/payment-requests/" + p["request_id"]
        assert c.get(path, headers=h).json()["provider_mode"] == "paypal_sandbox"
        assert c.post(path + "/approve", headers=h, json={"terms_sha256": p["terms_sha256"]}).status_code == 200
        assert c.post("/v1/lab" + path[3:] + "/execute", headers=h).status_code == 200
        backend.approve()
        response = c.post(path + "/capture", headers=h)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "provider_confirmed"
        assert len(reads) == 1
        assert reads[0].headers["Authorization"].startswith("Bearer ")
        view = c.get(path, headers=h).json()
        receipts = d.wallet_store.query("SELECT * FROM platform_paypal_read_receipts")
        if mode == "ok":
            assert len(receipts) == 1
            assert view["verification"]["verified_by"] == "paypal_read"
            events = c.get(path + "/timeline", headers=h).json()["events"]
            assert any(e["verified_by"] == "paypal_read" for e in events)
        else:
            assert view["verification"] is None
            assert not receipts
            if mode in {"down", "crash"}:
                assert view["status"] == "provider_confirmed"


def test_unexecuted_simulator_is_explicit_in_owner_payment_reads(tmp_path):
    with world(tmp_path) as (c, d):
        h = bearer(owner(d))
        p = c.post("/v1/lab/payment-requests", headers=h, json=payment()).json()
        assert c.get("/v1/payment-requests/" + p["request_id"], headers=h).json()["provider_mode"] == "simulated"
