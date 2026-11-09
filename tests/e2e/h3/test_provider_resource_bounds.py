"""Bounded upstream streaming, exact payout identities and sanitized logs."""
from copy import deepcopy
from decimal import Decimal
import io
import json
import logging

import httpx
import pytest

from agentpal.api import _JsonFormatter
from agentpal.infra_contracts import ProviderError
from agentpal.providers.paypal_payouts import PayPalPayoutsProvider
from agentpal.providers.paypal_webhooks import MAX_CERT_BYTES, WebhookVerificationError
from test_paypal_payouts import PayoutHTTP
from test_paypal_payouts_provider import TERMS
from test_provider_paypal_support import (
    CERT_URL, capture_event, make_config, make_provider, make_signing_cert, signed_headers,
)


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


# Fixed by the lead 2026-11-05 (was strict xfail E2E-H3-3).
def test_certificate_size_guard_stops_reading_before_unbounded_tail():
    key, _ = make_signing_cert()
    provider, fake = make_provider()
    stream = Stream([b'x' * (MAX_CERT_BYTES + 1), b'must-not-read-tail'])
    fake.routes[('GET', httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, stream=stream)
    raw = capture_event()
    try:
        with pytest.raises(WebhookVerificationError):
            provider.verify_webhook(signed_headers(key, raw), raw)
        assert stream.closed
        assert stream.reads == 1
    finally:
        provider.http_client.close()


@pytest.mark.parametrize('change', ['receiver', 'amount', 'currency', 'sender_item',
    'batch_id', 'note', 'status', 'item_id', 'pages'])
def test_payout_reads_cannot_confirm_substituted_or_ambiguous_items(change):
    backend = PayoutHTTP()
    with httpx.Client(transport=httpx.MockTransport(backend.handler)) as http:
        provider = PayPalPayoutsProvider(make_config(), http_client=http)
        batch = provider.create(TERMS)

        def altered(data):
            data = deepcopy(data)
            item = data['items'][0]
            payload = item['payout_item']
            if change == 'receiver':
                payload['receiver'] = 'other@example.com'
            elif change == 'amount':
                payload['amount']['value'] = '1.01'
            elif change == 'currency':
                payload['amount']['currency'] = 'EUR'
            elif change == 'sender_item':
                payload['sender_item_id'] = 'OTHER'
            elif change == 'batch_id':
                item['payout_batch_id'] = 'OTHER'
            elif change == 'note':
                payload['note'] = 'other approved purpose'
            elif change == 'status':
                item['transaction_status'] = 'SUCCESS\u200b'
            elif change == 'item_id':
                item['payout_item_id'] = '../OTHER'
            else:
                data['total_pages'] = 2
            return data

        backend.change = altered
        with pytest.raises(ProviderError):
            provider.read(batch, TERMS)
        assert len(backend.posts) == 1


def test_deep_payout_body_is_controlled_error_not_parser_crash():
    backend = PayoutHTTP()

    def handler(request):
        if request.url.path.endswith('/payouts'):
            return httpx.Response(201, content=b'{"x":' + b'[' * 6000 + b'0' + b']' * 6000 + b'}')
        return backend.handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = PayPalPayoutsProvider(make_config(), http_client=http)
        with pytest.raises(ProviderError) as error:
            provider.create(TERMS)
        assert error.value.outcome_unknown


def test_owned_log_formatter_redacts_credentials_and_prevents_newline_forgery(monkeypatch):
    sentinel = 'h3-configured-' + 'z' * 32
    monkeypatch.setenv('PAYPAL_SANDBOX_CLIENT_SECRET', sentinel)
    token = 'apo_' + 'a' * 24 + '_' + 'b' * 43
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter())
    record = logging.LogRecord('agentpal.h3', logging.ERROR, '', 0,
        'upstream text %s', ('Bearer ' + token + '\nforged-success ' + sentinel,), None)
    record.authorization = 'Bearer ' + token
    handler.handle(record)
    output = stream.getvalue()
    assert sentinel not in output and token not in output
    assert len(output.splitlines()) == 1
    assert json.loads(output)['authorization'] == '[REDACTED]'


@pytest.mark.parametrize('value', ['-1', 'NaN', '1e99999', '\uff11.00', '1.000000000'])
def test_refund_money_parser_rejects_noncanonical_values(value):
    from agentpal.providers.paypal_refunds import money
    with pytest.raises(ProviderError):
        money({'value': value, 'currency_code': 'USD'})
    assert money({'value': '1.00', 'currency_code': 'USD'}) == (Decimal('1.00'), 'USD')


# Fixed by the lead 2026-11-05 (was strict xfail E2E-H3-3).
def test_postback_verification_body_is_bounded_before_trusting_success():
    key, _ = make_signing_cert()
    provider, fake = make_provider(webhook_verification='postback')
    stream = Stream([b'{"verification_status":"SUCCESS","padding":"',
                     b'x' * (1024 * 1024 + 1), b'"}'])
    fake.routes[('POST', '/v1/notifications/verify-webhook-signature')] = lambda r: httpx.Response(
        200, stream=stream)
    raw = capture_event()
    refused = False
    try:
        try:
            provider.verify_webhook(signed_headers(key, raw), raw)
        except WebhookVerificationError:
            refused = True
        assert refused, 'Oversized postback verification success was trusted'
        assert stream.reads < 3
    finally:
        provider.http_client.close()
