"""OPTIONAL LIVE PayPal SANDBOX tests. Skipped unless sandbox credentials are set.

    PAYPAL_SANDBOX_CLIENT_ID=... PAYPAL_SANDBOX_CLIENT_SECRET=... \\
        python -m pytest -o addopts="" -q tests/test_provider_paypal_live.py

They talk to https://api-m.sandbox.paypal.com only (enforced by the provider config).
Capture is not exercised: it needs a sandbox buyer to approve the order in a browser.
A skipped run proves nothing about the live sandbox; report it as BLOCKED, not passing.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

from agentpal.infra_contracts import ProviderError, ProviderStatus
from agentpal.providers import PayPalSandboxProvider, load_config_from_env
from agentpal.spending import ApprovalTerms

LIVE = bool(os.environ.get("PAYPAL_SANDBOX_CLIENT_ID")
            and (os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET")
                 or os.environ.get("PAYPAL_SANDBOX_SECRET")))

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="LIVE PayPal sandbox test: PAYPAL_SANDBOX_CLIENT_ID / "
                                        "PAYPAL_SANDBOX_CLIENT_SECRET not set (BLOCKED)"),
]


@pytest.fixture(scope="module")
def provider():
    return PayPalSandboxProvider(load_config_from_env())


def test_live_sandbox_token(provider):
    assert provider.access_token()


def test_live_sandbox_create_and_read_order(provider):
    request_id = f"live_{uuid.uuid4().hex[:12]}"
    terms = ApprovalTerms(request_id=request_id, agent_id="agent_live", amount=Decimal("1.00"),
                          currency="USD", merchant_name="AgentPal sandbox test",
                          merchant_domain=None, purpose="sandbox smoke test, no real money")
    ref = provider.create_order(terms, idempotency_key=f"create-{uuid.uuid4()}")
    assert ref.environment == "sandbox"
    assert ref.status in (ProviderStatus.created, ProviderStatus.payer_action_required)
    assert ref.payer_action_url and ".sandbox.paypal.com" in ref.payer_action_url
    event = provider.get_order_status(ref.provider_order_id)
    assert event.verified and event.status is ref.status
    assert event.amount == Decimal("1.00") and event.currency == "USD"


def test_live_sandbox_capture_unapproved_order_is_a_definite_failure(provider):
    terms = ApprovalTerms(request_id=f"live_{uuid.uuid4().hex[:12]}", agent_id="agent_live",
                          amount=Decimal("1.00"), currency="USD",
                          merchant_name="AgentPal sandbox test", merchant_domain=None,
                          purpose="sandbox smoke test, no real money")
    ref = provider.create_order(terms, idempotency_key=f"create-{uuid.uuid4()}")
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(ref.provider_order_id, idempotency_key=f"capture-{uuid.uuid4()}")
    assert exc.value.outcome_unknown is False      # 422 ORDER_NOT_APPROVED expected
