"""Completion reference safety and complete status fingerprints."""

import pytest

from test_provider_paypal_support import make_provider, order_json


@pytest.mark.parametrize("url", [
    "https://user:password@www.sandbox.paypal.com/checkoutnow",
    "https://www.sandbox.paypal.com:8443/checkoutnow",
    "https://www.sandbox.paypal.com:bad/checkoutnow",
    "https://www.sandbox.paypal.com/checkout\nnow",
    "https://www.sandbox.paypal.com/checkout now",
])
def test_ambiguous_sandbox_payer_links_are_dropped(url):
    provider, _ = make_provider()
    assert provider._payer_link({"links": [{"rel": "approve", "href": url}]}) is None


def test_status_fingerprint_includes_currency():
    provider, _ = make_provider()
    first = provider.order_event(order_json(currency="USD"), b"usd", expected_order_id="5O190127TN364715T",
                                  event_type="ORDER_STATUS_READ")
    second = provider.order_event(order_json(currency="EUR"), b"eur", expected_order_id="5O190127TN364715T",
                                   event_type="ORDER_STATUS_READ")
    assert first.event_id != second.event_id
