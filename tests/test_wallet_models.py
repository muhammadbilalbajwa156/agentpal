"""Wallet models: a virtual allowance is never money; provider funds are not representable;
an agent's draft cannot carry a request id, agent id or budget figure."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentpal.contracts import AgentContext
from agentpal.schemas import ID_PATTERN
from agentpal.wallet import (
    InstrumentKind,
    OwnerTokenMap,
    PaymentDraft,
    PaymentInstrumentRef,
    SandboxExecution,
    WalletView,
    owner_principal_from,
)
from agentpal.wallet.models import SAFE_ID
from agentpal.wallet.principals import NotAuthorized
from agentpal.wallet.service import new_request_id
from agentpal.auth import Principal
from agentpal.workforce.roles import Role

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_provider_funds_are_not_representable():
    with pytest.raises(ValidationError, match="DESIGN ONLY"):
        PaymentInstrumentRef(instrument_id="inst_1", owner_id="owner_a",
                             kind=InstrumentKind.provider_funds, provider="paypal_sandbox")


def test_instrument_can_never_claim_real_money():
    with pytest.raises(ValidationError):
        PaymentInstrumentRef(instrument_id="inst_1", owner_id="owner_a",
                             kind=InstrumentKind.provider_sandbox_instrument,
                             provider="paypal_sandbox", is_real_money=True)
    with pytest.raises(ValidationError):
        PaymentInstrumentRef(instrument_id="inst_1", owner_id="owner_a",
                             kind=InstrumentKind.provider_sandbox_instrument,
                             provider="paypal_live")
    ok = PaymentInstrumentRef(instrument_id="inst_1", owner_id="owner_a",
                              kind=InstrumentKind.provider_sandbox_instrument,
                              provider="paypal_sandbox", external_ref="sb-acct-1")
    assert ok.is_real_money is False


def test_virtual_allowance_has_no_provider_reference():
    with pytest.raises(ValidationError, match="no provider reference"):
        PaymentInstrumentRef(instrument_id="inst_1", owner_id="owner_a",
                             kind=InstrumentKind.virtual_allowance,
                             provider="local_simulation", external_ref="x")


def test_wallet_view_and_simulation_cannot_be_real_money():
    with pytest.raises(ValidationError):
        WalletView(agent_id="a_1", owner_id="o_1", role=Role.payment,
                   policy_configuration="operator", allowances=[], instruments=[],
                   is_real_money=True)
    with pytest.raises(ValidationError):  # a simulation reference must be sim-prefixed
        SandboxExecution(request_id="r", simulation_reference="PAYID-123",
                         ledger_state="pending")


def test_wallet_view_has_no_balance_field():
    assert "balance" not in WalletView.model_fields
    from agentpal.wallet.models import CurrencyAllowance
    assert not any("balance" in f for f in CurrencyAllowance.model_fields)


@pytest.mark.parametrize("field,value", [("request_id", "req_x"), ("agent_id", "other"),
                                         ("monthly_budget", "999999"),
                                         ("budget_spent_this_period", "0")])
def test_draft_cannot_carry_server_or_operator_fields(field, value):
    with pytest.raises(ValidationError):
        PaymentDraft(amount=Decimal("1.00"), currency="USD", merchant_name="m",
                     purpose="p", **{field: value})


def test_draft_refuses_float_money():
    with pytest.raises(ValidationError, match="decimal string"):
        PaymentDraft(amount=1.1, currency="USD", merchant_name="m", purpose="p")


def test_server_request_ids_satisfy_both_id_patterns_and_are_unique():
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    for i in ids:
        assert re.fullmatch(ID_PATTERN, i) and re.fullmatch(SAFE_ID, i)
        AgentContext(agent_id=i[:64], owner_id="owner_a", role="payment")


def test_only_mapped_operator_tokens_become_owners():
    m = OwnerTokenMap(owners={"ops-1": "owner_a"})
    assert owner_principal_from(Principal(token_id="ops-1", role="operator"), m
                                ).owner_id == "owner_a"
    with pytest.raises(NotAuthorized, match="NOT_AN_OWNER"):
        owner_principal_from(Principal(token_id="agent-key", role="agent",
                                       agent_id="alice_payment"), m)
    with pytest.raises(NotAuthorized, match="NOT_AN_OWNER"):
        owner_principal_from(Principal(token_id="ops-2", role="operator"), m)
    with pytest.raises(NotAuthorized, match="UNAUTHENTICATED"):
        owner_principal_from(None, m)
    with pytest.raises(ValidationError):
        OwnerTokenMap(owners={"ops-1": "owner:with:colons"})
