"""Visible advisory reasoning comes from real Shopper tool calls and catalogue data."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from test_platform_workspace import bearer, owner, world
from test_shopper_agent import call


@pytest.mark.parametrize("offer_count", [1, 3, 5])
def test_lab_errand_exposes_plan_actual_candidates_and_choice(tmp_path, monkeypatch, offer_count):
    calls = [call("search_products", {"query": "office pencils", "max_price": "20.00"}),
             call("request_payment", {"offer_id": "offer_0", "amount": "2.00",
                                      "business_justification": "Lowest priced in-stock office pencil."})]
    seen = []

    def upstream(request):
        seen.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [calls.pop(0)]}}]})
        assert request.url.host == "api.trychannel3.com"
        return httpx.Response(200, json={"products": [{"title": f"Pencil {i}", "offers": [{
            "domain": f"store{i}.example", "availability": "InStock",
            "price": {"price": str(i + 2), "currency": "USD"},
            "url": f"https://store{i}.example/pencil"}]} for i in range(offer_count)]})

    settings = {"AGENTPAL_MASTER_KEY": Fernet.generate_key().decode(),
                "CHANNEL3_API_KEY": "synthetic-channel", "GROQ_API_KEY": "synthetic-model"}
    with world(tmp_path, settings) as (c, d):
        h = bearer(owner(d))
        original_client = httpx.Client
        monkeypatch.setattr("agentpal.platform.lab.httpx.Client", lambda **kw: original_client(
            **kw, transport=httpx.MockTransport(upstream)))
        task = "Please buy office pencils for the shared desk"
        response = c.post("/v1/lab/errands", headers=h, json={"task": task, "budget": "20.00"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["task"] == task
        events = result["events"]
        plans = [e for e in events if e.get("kind") == "plan"]
        assert plans and plans[0]["query"] == "office pencils"
        assert plans[0]["max_price"] == "20.00"
        candidates = [e for e in events if e.get("kind") == "candidate"]
        assert len(candidates) == min(offer_count, 3)
        for i, event in enumerate(candidates):
            assert event["title"] == f"Pencil {i}"
            assert event["amount"] == f"{i + 2}.00"
            assert event["merchant"] == f"store{i}.example"
            assert event["in_stock"] is True
        choice = next(e for e in events if e.get("kind") == "selection")
        assert choice["justification"] == "Lowest priced in-stock office pencil."
        assert any(e["message"] == "AI proposes · Rules decide · You approve" for e in events)
        view = c.get("/v1/payment-requests/" + result["request_id"], headers=h).json()
        assert view["status"] == "awaiting_approval"
        assert view["terms"]["purpose"] == task
        assert set(seen) == {"api.groq.com", "api.trychannel3.com"}
        assert "synthetic-channel" not in json.dumps(result)
        assert "synthetic-model" not in json.dumps(result)
