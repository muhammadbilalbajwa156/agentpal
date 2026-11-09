"""Bounded, read-only PayPal SANDBOX records. No lifecycle mutation or raw errors.

Transaction Search is eventually consistent; Orders v2 is the authoritative read.
https://developer.paypal.com/api/transaction-search/v1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

from .config import SANDBOX_BASE_URL
from .paypal_sandbox import PayPalSandboxProvider

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PAGES = 10
MAX_PAGE_SIZE = 100


class ReportingError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None, outcome_unknown: bool = False):
        super().__init__(code)
        self.code, self.status, self.outcome_unknown = code, status, outcome_unknown


def window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.utcoffset() is None or end.utcoffset() is None
            or not timedelta(0) < end - start <= timedelta(days=31)):
        raise ValueError('invalid_reporting_window')
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def bounded_int(value: int, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


class PayPalReporting:
    def __init__(self, provider: PayPalSandboxProvider):
        if not isinstance(provider, PayPalSandboxProvider):
            raise TypeError('sandbox_provider_required')
        self.provider = provider

    def request(self, method: str, path: str, *, params=None, body=None,
                request_id: str | None = None, missing_ok: bool = False) -> dict | None:
        """Internal fixed paths only. Stream before parsing; never follow redirects/retry."""
        if path not in {'/v1/notifications/webhooks', '/v1/reporting/transactions'} and not (
                method == 'GET' and re.fullmatch(r'/v2/checkout/orders/[A-Za-z0-9-]{1,128}', path)):
            raise ValueError('unsupported_paypal_operation')
        if method not in {'GET', 'POST'} or (method == 'POST' and (
                path != '/v1/notifications/webhooks' or not request_id)):
            raise ValueError('unsupported_paypal_operation')
        try:
            token = self.provider.access_token()
            headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
            if request_id:
                headers['PayPal-Request-Id'] = request_id
            with self.provider.http_client.stream(method, SANDBOX_BASE_URL + path,
                    headers=headers, params=params, json=body, timeout=self.provider.timeout,
                    follow_redirects=False) as response:
                if missing_ok and response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    raise ReportingError('paypal_unavailable', status=response.status_code,
                                         outcome_unknown=method != 'GET')
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ReportingError('paypal_response_invalid', outcome_unknown=method != 'GET')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ReportingError:
            raise
        except (ValueError, TypeError):
            raise ReportingError('paypal_response_invalid', outcome_unknown=method != 'GET') from None
        except Exception:
            # Includes OAuth errors: no upstream body or exception is reflected.
            raise ReportingError('paypal_unavailable', outcome_unknown=method != 'GET') from None

    def order(self, order_id: str) -> dict | None:
        if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', order_id):
            raise ValueError('invalid_order_id')
        order = self.request('GET', '/v2/checkout/orders/' + order_id, missing_ok=True)
        if order is not None and order.get('id') != order_id:
            raise ReportingError('paypal_order_identity')
        return order

    def transactions(self, start: datetime, end: datetime, *, page_size: int = MAX_PAGE_SIZE,
                     max_pages: int = MAX_PAGES) -> list[dict]:
        start, end = window(start, end)
        if not bounded_int(page_size, MAX_PAGE_SIZE) or not bounded_int(max_pages, MAX_PAGES):
            raise ValueError('invalid_reporting_pagination')
        rows = []
        for page in range(1, max_pages + 1):
            data = self.request('GET', '/v1/reporting/transactions', params={
                'start_date': start.isoformat(timespec='seconds'),
                'end_date': end.isoformat(timespec='seconds'), 'fields': 'transaction_info',
                'page_size': page_size, 'page': page})
            details, total = data.get('transaction_details'), data.get('total_pages')
            if details is None and total == 0 and data.get('total_items') == 0:
                details = []
            if (not isinstance(details, list) or len(details) > page_size
                    or type(total) is not int or total < 0
                    or data.get('page') != page
                    or any(not isinstance(d, dict) or not isinstance(d.get('transaction_info'), dict)
                           for d in details)):
                raise ReportingError('paypal_response_invalid')
            if total > max_pages:
                raise ReportingError('paypal_search_limit')
            rows.extend(d['transaction_info'] for d in details)
            if page >= total:
                return rows
        raise ReportingError('paypal_search_limit')
