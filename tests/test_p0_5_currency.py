"""P0.5 regression: currency-aware limits (review probe P05).

Before the fix, one numeric hard limit (5000) was applied to every currency, so
JPY 6,000 (~USD 40) was hard-blocked while GBP 4,999 passed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

CURRENCIES = {
    "USD": {"monthly_budget": "20000.00", "rolling_window_limit": "20000.00",
            "per_transaction_hard_limit": "5000.00", "per_transaction_review_limit": "1000.00"},
    "GBP": {"monthly_budget": "16000.00", "rolling_window_limit": "16000.00",
            "per_transaction_hard_limit": "4000.00", "per_transaction_review_limit": "800.00"},
    "JPY": {"monthly_budget": "3000000", "rolling_window_limit": "3000000",
            "per_transaction_hard_limit": "750000", "per_transaction_review_limit": "150000"},
}


def service() -> RiskService:
    sp = SpendPolicy.model_validate({
        "policy_version": "test",
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": CURRENCIES}}})
    return RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       clock=lambda: T0, spend_policy=sp)


def req(i, currency, amount, **kw) -> PaymentRequest:
    return PaymentRequest.model_validate({
        **VALID, "request_id": f"req_ccy_{i}", "currency": currency, "amount": amount,
        "recurring": False, "billing_interval": None, "monthly_budget": None,
        "merchant_name": f"Vendor {i}", "merchant_domain": f"vendor{i}.example.org", **kw})


def hard(a) -> list[str]:
    return a.hard_violations


def test_jpy_uses_jpy_limits_not_usd_numbers():
    s = service()
    assert "AMOUNT_ABOVE_HARD_LIMIT" not in hard(s.assess(req(1, "JPY", 6000)))
    assert "AMOUNT_ABOVE_HARD_LIMIT" in hard(s.assess(req(2, "JPY", 750001)))


def test_gbp_uses_gbp_limits():
    s = service()
    a = s.assess(req(3, "GBP", "4999.00"))
    assert "AMOUNT_ABOVE_HARD_LIMIT" in hard(a)  # GBP hard limit is 4000, not 5000
    assert not hard(s.assess(req(4, "GBP", "3999.99")))


def test_usd_boundary():
    s = service()
    assert "AMOUNT_ABOVE_HARD_LIMIT" not in hard(s.assess(req(5, "USD", "5000.00")))
    assert "AMOUNT_ABOVE_HARD_LIMIT" in hard(s.assess(req(6, "USD", "5000.01")))


def test_review_limit_is_per_currency():
    s = service()
    codes = {x.code for x in s.assess(req(7, "JPY", 200000)).signals}
    assert "AMOUNT_ABOVE_REVIEW_LIMIT" in codes
    codes = {x.code for x in s.assess(req(8, "USD", "900.00")).signals}
    assert "AMOUNT_ABOVE_REVIEW_LIMIT" not in codes


def test_equivalent_value_transactions_follow_each_currencys_policy():
    """~USD 100 in three currencies: each judged only against its own configuration."""
    s = service()
    results = {c: s.assess(req(10 + i, c, amt))
               for i, (c, amt) in enumerate([("USD", "100.00"), ("GBP", "80.00"),
                                             ("JPY", 15000)])}
    assert all(not a.hard_violations for a in results.values())
    for c in results:
        totals = s.store.spend_totals("agent_demo_001", c, T0)
        assert totals["completed"] == 0
    assert s.store.spend_totals("agent_demo_001", "JPY", T0)["committed"] == Decimal("15000")
    assert s.store.spend_totals("agent_demo_001", "USD", T0)["committed"] == Decimal("100.00")


def test_currency_without_configured_policy_is_blocked_not_mapped():
    s = service()
    a = s.assess(req(20, "CAD", "10.00"))  # CAD is globally allowed, not configured
    assert "NO_SPENDING_POLICY" in hard(a) and a.recommendation == "block"
    nsp = next(x for x in a.signals if x.code == "NO_SPENDING_POLICY")
    assert nsp.evidence["currency"] == "CAD"


def test_disallowed_and_unsupported_currencies():
    s = service()
    assert "CURRENCY_NOT_ALLOWED" in hard(s.assess(req(21, "CHF", "10.00")))
    with pytest.raises(ValidationError):
        req(22, "XXX", "10.00")


@pytest.mark.parametrize("currency,amount", [("JPY", "100.5"), ("USD", "10.001"),
                                             ("GBP", "1.234")])
def test_currency_decimal_precision_enforced(currency, amount):
    with pytest.raises(ValidationError):
        req(30, currency, amount)


def test_config_amounts_must_respect_minor_units_and_be_exact():
    bad_jpy = {"JPY": {**CURRENCIES["JPY"], "per_transaction_hard_limit": "100.5"}}
    with pytest.raises(ValidationError):
        SpendPolicy.model_validate({"policy_version": "t", "agents": {
            "a": {"owner_id": "o", "currencies": bad_jpy}}})
    float_usd = {"USD": {**CURRENCIES["USD"], "monthly_budget": 100.0}}
    with pytest.raises(ValidationError):
        SpendPolicy.model_validate({"policy_version": "t", "agents": {
            "a": {"owner_id": "o", "currencies": float_usd}}})


def test_budget_is_tracked_separately_per_currency():
    s = service()
    s.assess(req(40, "USD", "4000.00"))
    a = s.assess(req(41, "JPY", 4000))  # same number, different currency: no interaction
    assert not a.hard_violations
    assert s.store.ledger_entry("req_ccy_41")["currency"] == "JPY"
