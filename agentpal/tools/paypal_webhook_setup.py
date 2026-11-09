"""List/create SANDBOX app webhooks. No real registration is performed by tests.

python -m agentpal.tools.paypal_webhook_setup --list
python -m agentpal.tools.paypal_webhook_setup --url https://public.example/v1/providers/paypal/webhooks
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

from ..providers.config import load_config_from_env
from ..providers.paypal_sandbox import PayPalSandboxProvider
from ..providers.paypal_reporting import PayPalReporting, ReportingError

EVENT_TYPES = (
    'CHECKOUT.ORDER.APPROVED', 'PAYMENT.CAPTURE.COMPLETED', 'PAYMENT.CAPTURE.DENIED',
    'PAYMENT.CAPTURE.DECLINED', 'PAYMENT.CAPTURE.PENDING', 'PAYMENT.CAPTURE.REFUNDED',
    'PAYMENT.CAPTURE.REVERSED', 'PAYMENT.PAYOUTS-ITEM.SUCCEEDED', 'PAYMENT.PAYOUTS-ITEM.FAILED',
    'PAYMENT.PAYOUTS-ITEM.UNCLAIMED', 'PAYMENT.PAYOUTS-ITEM.RETURNED',
    'PAYMENT.PAYOUTS-ITEM.BLOCKED', 'PAYMENT.PAYOUTS-ITEM.DENIED', 'PAYMENT.PAYOUTS-ITEM.HELD',
    'CUSTOMER.DISPUTE.CREATED', 'CUSTOMER.DISPUTE.UPDATED', 'CUSTOMER.DISPUTE.RESOLVED',
)
PATH = '/v1/notifications/webhooks'


def validate_public_url(url: str) -> str:
    try:
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
            raise ValueError
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None
                or parts.fragment or host.rstrip('.').lower() == 'localhost'
                or host.lower().endswith(('.localhost', '.local', '.internal'))):
            raise ValueError
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
        return url
    except (ValueError, OSError):
        raise ValueError('public_https_url_required') from None


class WebhookSetup:
    def __init__(self, reporting: PayPalReporting):
        self.reporting = reporting

    def list(self) -> list[dict]:
        rows = self.reporting.request('GET', PATH).get('webhooks')
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(r, dict) for r in rows):
            raise ReportingError('paypal_response_invalid')
        return rows

    @staticmethod
    def _id(row: dict) -> str:
        value = row.get('id')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value):
            raise ReportingError('paypal_response_invalid')
        return value

    def ensure(self, url: str) -> str:
        url = validate_public_url(url)
        for row in self.list():
            if row.get('url') == url:
                names = {e.get('name') for e in row.get('event_types', []) if isinstance(e, dict)}
                if not set(EVENT_TYPES) <= names:
                    raise ReportingError('webhook_events_incomplete')
                return self._id(row)
        body = {'url': url, 'event_types': [{'name': e} for e in EVENT_TYPES]}
        key = 'agentpal-webhook-' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        row = self.reporting.request('POST', PATH, body=body, request_id=key)
        if row.get('url') != url:
            raise ReportingError('paypal_response_invalid', outcome_unknown=True)
        return self._id(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true')
    mode.add_argument('--url')
    args = parser.parse_args()
    provider = None
    try:
        if args.url:
            validate_public_url(args.url)
        provider = PayPalSandboxProvider(load_config_from_env())
        setup = WebhookSetup(PayPalReporting(provider))
        if args.list:
            print(json.dumps({'webhook_ids': [setup._id(r) for r in setup.list()]}))
        else:
            print('PAYPAL_SANDBOX_WEBHOOK_ID=' + setup.ensure(args.url))
        return 0
    except Exception:
        print(json.dumps({'error': 'sandbox_webhook_setup_failed',
                          'next': 'inspect existing subscriptions with --list before retrying'}))
        return 1
    finally:
        if provider is not None:
            provider.http_client.close()


if __name__ == '__main__':
    raise SystemExit(main())
