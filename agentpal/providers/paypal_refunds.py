"""Bounded sandbox-only Payments v2 adapter. Never retries a mutation.

Provider private HTTP access is confined to this adapter so response bodies can be
streamed with a hard bound (the shared Orders helper buffers entire responses).
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

import httpx

from ..infra_contracts import ProviderError
from .config import SANDBOX_BASE_URL
from .paypal_sandbox import PayPalSandboxProvider

MAX_RESPONSE_BYTES = 256 * 1024
_ID = re.compile(r'^[A-Za-z0-9-]{1,128}$')
_MONEY = re.compile(r'^[0-9]{1,10}(\.[0-9]{1,8})?$')


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
    return value


def money(value):
    if (not isinstance(value, dict) or not isinstance(value.get('value'), str)
            or not _MONEY.fullmatch(value['value'])
            or not isinstance(value.get('currency_code'), str)
            or not re.fullmatch(r'[A-Z]{3}', value['currency_code'])):
        raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
    return Decimal(value['value']), value['currency_code']


def request_key(request_id: str, key: str) -> str:
    encoded = json.dumps([request_id, key, 'refund'], separators=(',', ':')).encode()
    return 'agentpal-refund-' + hashlib.sha256(encoded).hexdigest()[:23]


class PayPalRefunds:
    def __init__(self, provider):
        if (not isinstance(provider, PayPalSandboxProvider)
                or provider.environment != 'sandbox' or provider.name != 'paypal'
                or provider._base_url != SANDBOX_BASE_URL):
            raise ProviderError('refund_provider_unavailable', outcome_unknown=False)
        self.provider = provider

    def _call(self, method, path, *, body=None, key=None):
        """Only fixed API paths; no response link is ever followed. TLS defaults preserved."""
        p = self.provider
        headers = {'Authorization': 'Bearer ' + p.access_token(), 'Accept': 'application/json',
                   'Prefer': 'return=representation'}
        if key:
            headers['PayPal-Request-Id'] = key
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with p._http.stream(method, SANDBOX_BASE_URL + path, headers=headers,
                                content=None if body is None else json.dumps(body).encode(),
                                timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as r:
                raw = bytearray()
                for chunk in r.iter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderError('refund_response_too_large', outcome_unknown=True)
                    raw.extend(chunk)
                if not 200 <= r.status_code < 300:
                    if r.status_code == 401:
                        p.invalidate_token()
                    if r.status_code == 403:
                        # PayPal refuses to refund a capture paid to ANOTHER merchant unless
                        # that merchant granted this app refund permission (E2E-T1-004).
                        raise ProviderError('refund_permission_denied', outcome_unknown=False)
                    raise ProviderError('refund_provider_error',
                                        outcome_unknown=r.status_code >= 500 or r.status_code < 400
                                        or r.status_code == 409)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
                return data
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise ProviderError('refund_provider_unavailable', outcome_unknown=True) from None

    def capture(self, capture_id, amount, currency):
        capture_id = identifier(capture_id)
        data = self._call('GET', f'/v2/payments/captures/{capture_id}')
        if (data.get('id') != capture_id or money(data.get('amount')) != (amount, currency)
                or data.get('status') not in ('COMPLETED', 'PARTIALLY_REFUNDED')):
            raise ProviderError('refund_capture_invalid', outcome_unknown=True)
        return data

    def refund(self, capture_id, amount, currency, *, key, invoice_id, reason=None):
        body = {'amount': {'value': format(amount, 'f'), 'currency_code': currency},
                'invoice_id': invoice_id}
        if reason:
            body['note_to_payer'] = reason
        return self._call('POST', f'/v2/payments/captures/{identifier(capture_id)}/refund',
                          body=body, key=key)

    def read(self, refund_id):
        return self._call('GET', f'/v2/payments/refunds/{identifier(refund_id)}')

    def discover(self, order_id, invoice_id):
        data = self._call('GET', f'/v2/checkout/orders/{identifier(order_id)}')
        if data.get('id') != order_id:
            raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
        units = data.get('purchase_units', [])
        if not isinstance(units, list) or len(units) != 1:
            raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
        refunds = units[0].get('payments', {}).get('refunds', [])
        if not isinstance(refunds, list) or len(refunds) > 100:
            raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
        matches = []
        for item in refunds:
            result = self.read(identifier(item.get('id')))
            if result.get('invoice_id') == invoice_id:
                matches.append(result)
        if len(matches) != 1:
            raise ProviderError('refund_not_discovered', outcome_unknown=True)
        return matches[0]

    @staticmethod
    def verify(data, *, capture_id, amount, currency, refund_id=None):
        rid = identifier(data.get('id'))
        status = data.get('status')
        links = data.get('links', [])
        path = '/v2/payments/captures/' + identifier(capture_id)
        # Real sandbox responses use the api.sandbox alias. These references are
        # compared exactly and NEVER fetched; all I/O remains on api-m.sandbox.
        expected = {SANDBOX_BASE_URL + path, 'https://api.sandbox.paypal.com' + path}
        up = [link.get('href') for link in links if isinstance(link, dict)
              and link.get('rel') == 'up'] if isinstance(links, list) and len(links) <= 20 else []
        if (status not in ('COMPLETED', 'PENDING', 'FAILED', 'CANCELLED')
                or money(data.get('amount')) != (amount, currency)
                or len(up) != 1 or up[0] not in expected or (refund_id is not None and rid != refund_id)):
            raise ProviderError('refund_evidence_invalid', outcome_unknown=True)
        return {'provider_refund_id': rid, 'status': status,
                'evidence_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}

    def disputes(self):
        data = self._call('GET', '/v1/customer/disputes?page_size=20')
        items = data.get('items', [])
        if not isinstance(items, list) or len(items) > 20:
            raise ProviderError('dispute_evidence_invalid', outcome_unknown=True)
        # PayPal defaults to summaries without transaction identifiers. Resolve a
        # bounded page through authenticated details before owner scoping.
        detailed = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if 'disputed_transactions' not in item:
                did = identifier(item.get('dispute_id'))
                item = self._call('GET', f'/v1/customer/disputes/{did}')
                if item.get('dispute_id') != did:
                    raise ProviderError('dispute_evidence_invalid', outcome_unknown=True)
            detailed.append(item)
        return detailed
