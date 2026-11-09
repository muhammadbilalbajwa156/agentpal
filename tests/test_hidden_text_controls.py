"""Lead fix 2026-11-06 (UI-T2 MEDIUM-1): agent-supplied text must not carry bidi overrides or
invisible format characters, which can make the owner see different terms than the ones
bound by terms_sha256. Rejected at the API edge for every /v1 input model."""
import pytest

from test_platform_fixtures import OWNER_A, build_world

HIDDEN = ["‮", "‭", "⁦", "⁧", "⁨", "⁩", "​", "‎",
          "‏", "؜", "﻿", "⁠", "\x07", "\x1b"]


@pytest.fixture
def w():
    return build_world()


@pytest.mark.parametrize("ch", HIDDEN)
@pytest.mark.parametrize("field", ["merchant_name", "purpose", "business_justification"])
def test_payment_text_with_hidden_controls_is_refused(w, ch, field):
    _, key = w.register()
    r = w.pay(key, **{field: "Good Shop" + ch + " moc.live"})
    assert r.status_code == 422, r.text


def test_normal_text_emoji_and_rtl_scripts_still_work(w):
    agent_id, key = w.register()
    w.wallet(agent_id)
    r = w.pay(key, merchant_name="\u0645\u062a\u062c\u0631 \U0001F468\u200d\U0001F469\u200d\U0001F467 Caf\u00e9",
              purpose="\u05e9\u05dc\u05d5\u05dd office supplies")
    assert r.status_code != 422, r.text


def test_agent_name_with_override_is_refused(w):
    r = w.client.post("/v1/agents", json={"name": "Shopper‮gnp.exe"},
                      headers={"Authorization": "Bearer " + OWNER_A})
    assert r.status_code == 422


def test_owner_sees_agent_context_and_agent_does_not(w):
    """UI-T2 LOW-2: the owner decides with the agent's own justification in view."""
    agent_id, key = w.register()
    w.wallet(agent_id)
    r = w.pay(key, business_justification="Team needs a quiet mouse for the new hire",
              product_query="Logitech MX Master 3S", product_url="https://shop.example/mx3s")
    assert r.status_code == 201, r.text
    rid = r.json()["request_id"]
    owner = w.client.get(f"/v1/payment-requests/{rid}", headers={"Authorization": "Bearer " + OWNER_A}).json()
    assert owner["agent_context"] == {"business_justification": "Team needs a quiet mouse for the new hire",
                                      "product_query": "Logitech MX Master 3S",
                                      "product_url": "https://shop.example/mx3s"}
    agent = w.client.get(f"/v1/payment-requests/{rid}", headers={"Authorization": "Bearer " + key}).json()
    assert agent.get("agent_context") is None
