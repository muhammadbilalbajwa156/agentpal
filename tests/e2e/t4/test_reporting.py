"""Exact offline oracle for read-only operations report correctness."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentpal.platform.paypal_ops import compare


@pytest.mark.parametrize("local,capture", [
    ("provider_confirmed", "PARTIALLY_REFUNDED"),
    ("reversed", "REFUNDED"),
])
def test_refunded_capture_is_not_reported_as_a_lifecycle_mismatch(local, capture):
    view = {"status": local, "terms": {"amount": Decimal("1.00"), "currency": "USD"}}
    money = {"value": "1.00", "currency_code": "USD"}
    order = {"status": "COMPLETED", "purchase_units": [{"amount": money,
        "payments": {"captures": [{"id": "T4CAPTURE", "status": capture, "amount": money}]}}]}
    actual = compare(view, order, [], datetime.now(timezone.utc))
    assert actual == ("pending", "refund_extension_required"), actual
