from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentpal.schemas import PaymentRequest, normalize_domain


def test_valid_request_is_normalized(payload):
    payload.update(currency=" usd ", merchant_domain="HTTPS://www.Example.com/billing?x=1")
    req = PaymentRequest.model_validate(payload)
    assert req.currency == "USD"
    assert req.merchant_domain == "example.com"
    assert req.amount == Decimal("49.00")
    assert req.direction.value == "outgoing"


@pytest.mark.parametrize("amount", [0, -1, -0.01, "abc", None, True, float("nan"),
                                    float("inf"), "1e12", 10.001])
def test_bad_amounts_rejected(payload, amount):
    payload["amount"] = amount
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


def test_amount_string_decimal_accepted(payload):
    payload["amount"] = "49.5"
    assert PaymentRequest.model_validate(payload).amount == Decimal("49.50")


def test_zero_decimal_currency(payload):
    payload.update(currency="JPY", amount=500)
    assert PaymentRequest.model_validate(payload).amount == Decimal("500")
    payload["amount"] = 500.5
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("currency", "XXX"), ("currency", "US"), ("request_id", "a"), ("request_id", "bad id!"),
    ("agent_id", ""), ("merchant_name", ""), ("purpose", ""), ("monthly_budget", -10),
    ("billing_interval", "hourly"), ("merchant_domain", "not a domain"),
    ("merchant_domain", "localhost"), ("merchant_domain", "user:pw@evil.com"),
])
def test_invalid_fields_rejected(payload, field, value):
    payload[field] = value
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


def test_missing_required_fields(payload):
    for field in ("request_id", "agent_id", "amount", "currency", "merchant_name", "purpose"):
        data = dict(payload)
        data.pop(field)
        with pytest.raises(ValidationError):
            PaymentRequest.model_validate(data)


def test_unknown_fields_rejected(payload):
    payload["approve"] = True
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


def test_interval_without_recurring_is_inconsistent(payload):
    payload.update(recurring=False, billing_interval="monthly")
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


def test_expiry_before_creation_rejected(payload):
    payload.update(created_at="2026-10-01T12:00:00Z", expires_at="2026-10-01T11:00:00Z")
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)


def test_naive_timestamps_rejected_offsets_normalized_to_utc(payload):
    # P0.3 behaviour change (documented): naive timestamps were previously assumed to be
    # UTC; an ambiguous timestamp is now rejected instead of silently interpreted.
    payload["created_at"] = "2026-10-01T12:00:00"
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate(payload)
    payload["created_at"] = "2026-10-01T17:00:00+05:00"
    req = PaymentRequest.model_validate(payload)
    assert req.created_at.utcoffset().total_seconds() == 0 and req.created_at.hour == 12


def test_oversized_text_and_evidence_rejected(payload):
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate({**payload, "business_justification": "x" * 5000})
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate({**payload, "evidence": [{"type": "note"}] * 21})


def test_idn_domain_converted_to_punycode():
    assert normalize_domain("exämple.com").startswith("xn--")
