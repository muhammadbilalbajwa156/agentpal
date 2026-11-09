"""Owner-scoped, read-only reconciliation. Never settles or releases a ledger hold.

P2/P3 extension point: refund/payout records need their own owned references and
comparisons. They must not be inferred from account-wide Transaction Search rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel

from ..providers.paypal_reporting import PayPalReporting, ReportingError, window
from ..providers.paypal_sandbox import PayPalSandboxProvider, parse_time
from .deps import ProviderReferenceReader
from .errors import PlatformError, not_configured

MAX_REQUESTS = 100


class ReconciliationRow(BaseModel):
    request_id: str
    lifecycle: str
    result: Literal['matched', 'mismatch', 'missing_at_paypal', 'pending']
    reason: str
    provider_order_id: str | None = None
    paypal_status: str | None = None


class PaypalReconciliation(BaseModel):
    rows: list[ReconciliationRow]
    start: datetime
    end: datetime
    payment_environment: Literal['sandbox'] = 'sandbox'
    is_real_money: Literal[False] = False
    read_only: Literal[True] = True


def money(value) -> tuple[Decimal, str] | None:
    if not isinstance(value, dict):
        return None
    amount, currency = value.get('value'), value.get('currency_code')
    if (not isinstance(amount, str) or not re.fullmatch(r'\d{1,12}(?:\.\d{1,3})?', amount)
            or not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency)):
        return None
    amount = Decimal(amount)
    return (amount, currency) if amount > 0 else None


def compare(view: dict, order: dict, transactions: list[dict] | None, now: datetime) -> tuple[str, str]:
    units = order.get('purchase_units')
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
        return 'pending', 'paypal_order_ambiguous'
    unit = units[0]
    payment = unit.get('payments', {})
    if not isinstance(payment, dict):
        return 'pending', 'paypal_order_ambiguous'
    captures = payment.get('captures', [])
    if not isinstance(captures, list) or len(captures) > 1 or any(not isinstance(c, dict) for c in captures):
        return 'pending', 'paypal_order_ambiguous'
    terms = view['terms']
    for observed in [unit.get('amount')] + [c.get('amount') for c in captures]:
        parsed = money(observed)
        if parsed is None:
            return 'pending', 'paypal_money_invalid'
        if parsed[1] != terms['currency']:
            return 'mismatch', 'currency_mismatch'
        if parsed[0] != terms['amount']:
            return 'mismatch', 'amount_mismatch'
    status, local = order.get('status'), view['status']
    capture = captures[0] if captures else {}
    capture_status = capture.get('status')
    completed = status == 'COMPLETED' and capture_status == 'COMPLETED'
    # E2E-T4-4: a (partially) refunded capture is no longer COMPLETED at PayPal by design;
    # refunds are reconciled by the refund records, so never call it a lifecycle mismatch.
    if local == 'reversed' or capture_status in {'REFUNDED', 'PARTIALLY_REFUNDED'}:
        return 'pending', 'refund_extension_required'
    if local == 'provider_confirmed' and not completed:
        return 'mismatch', 'lifecycle_mismatch'
    if completed and local != 'provider_confirmed':
        return 'mismatch', 'lifecycle_mismatch'
    if not completed:
        if status == 'VOIDED' or capture_status in {'DENIED', 'DECLINED', 'FAILED'}:
            return ('matched', 'provider_failed') if local in {'failed', 'rejected'} else ('mismatch', 'lifecycle_mismatch')
        return 'pending', 'awaiting_paypal_completion'
    capture_id = capture.get('id')
    if not isinstance(capture_id, str) or not re.fullmatch('[A-Za-z0-9-]{1,128}', capture_id):
        return 'pending', 'paypal_capture_invalid'
    occurred = parse_time(capture.get('update_time')) or parse_time(capture.get('create_time'))
    if transactions is None:
        if occurred is None or now - occurred <= timedelta(hours=3):
            return 'pending', 'paypal_search_pending'
        return 'pending', 'paypal_search_unavailable'
    matches = [t for t in transactions if t.get('transaction_id') == capture_id]
    if len(matches) > 1:
        return 'pending', 'paypal_search_ambiguous'
    if matches:
        parsed = money(matches[0].get('transaction_amount'))
        if parsed is None:
            return 'pending', 'paypal_money_invalid'
        if parsed[1] != terms['currency']:
            return 'mismatch', 'search_currency_mismatch'
        if parsed[0] != terms['amount']:
            return 'mismatch', 'search_amount_mismatch'
        if matches[0].get('transaction_status') != 'S':
            return 'mismatch', 'search_status_mismatch'
        return 'matched', 'verified_order_and_search'
    occurred = parse_time(capture.get('update_time')) or parse_time(capture.get('create_time'))
    if occurred is None or now - occurred <= timedelta(hours=3):
        return 'pending', 'paypal_search_pending'
    return 'mismatch', 'paypal_search_missing'


def reconcile(platform, owner, start: datetime, end: datetime) -> PaypalReconciliation:
    try:
        start, end = window(start, end)
    except ValueError:
        raise PlatformError(422, 'invalid_reporting_window') from None
    desk = platform.payments
    lifecycle, provider = desk.deps.lifecycle, desk.deps.paypal_for(owner.owner_id)
    if not isinstance(lifecycle, ProviderReferenceReader) or not isinstance(provider, PayPalSandboxProvider):
        raise not_configured()
    # Apply owner and date filters before reading any provider references or records.
    metadata = desk.store.query('SELECT request_id FROM platform_payment_meta WHERE owner_id=? '
        'AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?) '
        'ORDER BY created_at, request_id LIMIT ?', (owner.owner_id, start.isoformat(), end.isoformat(), MAX_REQUESTS + 1))
    if len(metadata) > MAX_REQUESTS:
        raise PlatformError(422, 'reconciliation_request_limit')
    rows = []
    reader = PayPalReporting(provider)
    search = None
    searched = False
    for meta in metadata:
        rid = meta['request_id']
        view = desk.get_for_owner(owner, rid)  # recheck ownership; use trusted exact terms
        refs = lifecycle.provider_references(rid)
        base = {'request_id': rid, 'lifecycle': getattr(view['status'], 'value', view['status'])}
        if not refs:
            submitted = view['status'] in {'provider_confirmed', 'provider_pending',
                'submitted_to_provider', 'reconciliation_required', 'reversed'}
            rows.append(ReconciliationRow(**base, result='pending' if submitted else 'matched',
                reason='provider_reference_missing' if submitted else 'not_submitted'))
            continue
        if len(refs) != 1 or refs[0].provider != provider.name or refs[0].environment != 'sandbox':
            rows.append(ReconciliationRow(**base, result='pending', reason='provider_reference_ambiguous'))
            continue
        oid = refs[0].provider_order_id
        base['provider_order_id'] = oid
        try:
            order = reader.order(oid)
        except (ReportingError, ValueError):
            rows.append(ReconciliationRow(**base, result='pending', reason='paypal_order_unavailable'))
            continue
        if order is None:
            rows.append(ReconciliationRow(**base, result='missing_at_paypal', reason='paypal_order_not_found'))
            continue
        # Search only when a capture can be corroborated. Never return account-wide rows.
        if order.get('status') == 'COMPLETED' and not searched:
            searched = True
            try:
                search = reader.transactions(start, end)
            except ReportingError:
                search = None
        result, reason = compare(view, order, search, platform.clock())
        status = order.get('status')
        safe_status = status if status in {'CREATED', 'SAVED', 'APPROVED', 'VOIDED', 'COMPLETED', 'PAYER_ACTION_REQUIRED'} else None
        rows.append(ReconciliationRow(**base, result=result, reason=reason, paypal_status=safe_status))
    return PaypalReconciliation(rows=rows, start=start, end=end)
