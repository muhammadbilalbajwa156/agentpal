"""SANDBOX scenario world through the REAL /v1 app. No real money.

python -m agentpal.demo.paypal_world [--live] [--report /tmp/paypal-world]
Browser automation is a labelled TEST BUYER; authenticated owner approval remains
an independent app action. Only verified provider outcomes count as completion.
P1 extension: pass an exact, approval-bound payee once its API contract is merged.
P2/P3 extension: add refunds/payouts after their owner-approved APIs are integrated.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import time
from tempfile import TemporaryDirectory
import uuid

import httpx

from ..api import create_app
from ..auth import TokenRegistry
from ..config import Settings
from ..platform.wiring import build_platform
from ..providers.config import PayPalSandboxConfig, load_config_from_env
from ..providers.paypal_sandbox import PayPalSandboxProvider
from .paypal_flow import DemoError


class WorldPayPal:
    """Offline HTTP fixture; all identities, approvals, budgets and ledger are REAL."""
    def __init__(self):
        self.orders: dict[str, dict] = {}

    def approve(self, order_id: str) -> None:
        self.orders[order_id]['status'] = 'APPROVED'

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != 'api-m.sandbox.paypal.com':
            raise DemoError('non_sandbox_request')
        if request.url.path == '/v1/oauth2/token':
            return httpx.Response(200, json={'access_token': 'fake-world-token', 'expires_in': 3600})
        if request.url.path == '/v1/reporting/transactions':
            rows = []
            for order in self.orders.values():
                for capture in order['purchase_units'][0].get('payments', {}).get('captures', []):
                    rows.append({'transaction_info': {'transaction_id': capture['id'],
                        'transaction_status': 'S', 'transaction_amount': capture['amount']}})
            return httpx.Response(200, json={'transaction_details': rows, 'total_pages': 1, 'page': 1})
        if request.url.path == '/v2/checkout/orders' and request.method == 'POST':
            body = json.loads(request.content)
            oid = 'WORLD-ORDER-' + str(len(self.orders) + 1)
            units = body['purchase_units']
            for unit in units:  # like PayPal: echo the requested payee, else the app account
                unit.setdefault('payee', {'email_address': 'world-app@business.example.com',
                                          'merchant_id': 'WORLDAPP0001'})
            order = {'id': oid, 'status': 'CREATED', 'purchase_units': units,
                'links': [{'rel': 'approve', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=' + oid}]}
            self.orders[oid] = order
            return httpx.Response(201, json=order)
        segments = request.url.path.split('/')
        oid = segments[4] if len(segments) >= 5 else ''
        order = self.orders.get(oid)
        if order is None:
            return httpx.Response(404, json={'name': 'RESOURCE_NOT_FOUND'})
        if request.method == 'GET':
            return httpx.Response(200, json=order)
        if request.method == 'POST' and segments[-1] == 'capture':
            if order['status'] != 'APPROVED':
                return httpx.Response(422, json={'name': 'UNPROCESSABLE_ENTITY',
                    'details': [{'issue': 'ORDER_NOT_APPROVED'}]})
            unit = order['purchase_units'][0]
            unit['payments'] = {'captures': [{'id': 'WORLD-CAPTURE-' + oid[-1],
                'status': 'COMPLETED', 'amount': unit['amount'],
                'update_time': datetime.now(timezone.utc).isoformat()}]}
            order['status'] = 'COMPLETED'
            return httpx.Response(201, json=order)
        raise DemoError('unexpected_offline_operation')


def redacted_id(value: str) -> str:
    return '…' + value[-4:]


async def run_world(*, live: bool = False, report_dir: str | Path | None = None,
                    buyer=None) -> dict:
    if live:
        load_config_from_env()  # fail closed before app or browser actions
    backend = None if live else WorldPayPal()
    calls = Counter()
    started = time.monotonic()
    run_start = datetime.now(timezone.utc)
    report_path = Path(report_dir) if report_dir is not None else None
    if report_path:
        report_path.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='agentpal-world-') as directory:
        settings = Settings(db_path=str(Path(directory) / 'risk.db'), auth_mode='required',
            analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'], log_level='ERROR',
            rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=1000)
        # Do not load optional market services or unrelated DB overrides in this demo.
        env = {}
        if live:
            import os
            env = {k: v for k, v in os.environ.items() if k.startswith('PAYPAL_SANDBOX_')}
        deps = build_platform(settings=settings, environ=env)
        try:
            def count_call(request):
                if request.method == 'POST' and request.url.path == '/v2/checkout/orders':
                    calls['create'] += 1
                if request.method == 'POST' and request.url.path.endswith('/capture'):
                    calls['capture'] += 1
            config = deps.provider.config if live else PayPalSandboxConfig(
                client_id='fake-world-client', client_secret='fake-world-secret', webhook_id='WORLD-HOOK')
            old_provider = deps.provider
            provider = PayPalSandboxProvider(config, http_client=httpx.Client(
                transport=httpx.MockTransport(backend.handler) if backend else None,
                event_hooks={'request': [count_call]}))
            if isinstance(old_provider, PayPalSandboxProvider):
                old_provider.http_client.close()
            deps.provider = provider
            deps.lifecycle.provider = provider
            identity = deps.owner_authenticator
            identity.create_owner('world_owner')
            owner_token, _ = identity.issue_token('world_owner')
            owner_headers = {'Authorization': 'Bearer ' + owner_token}
            app = create_app(service=deps.risk_service, settings=settings,
                             registry=TokenRegistry(), platform=deps)
            personas = json.loads(Path(__file__).with_name('paypal_sandbox_personas.json').read_text())['personas']
            normal_name = next(p['name'] for p in personas if p['name'] == 'Northwind Office Supply')
            payee_of = {p['name']: p['email'] for p in personas
                        if p.get('account_type') == 'BUSINESS' and p.get('role') == 'merchant'}
            risky_name = next(p['name'] for p in personas if p['name'] == 'Shady Deals Outlet')
            scenarios = []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as client:
                async def call(method, path, *, headers=None, expected=(200,), **kwargs):
                    response = await client.request(method, path, headers=owner_headers if headers is None else headers, **kwargs)
                    if response.status_code not in expected:
                        step = re.sub(r'(pay|payout)_[0-9a-f]{8,}', r'\1_…', method + ' ' + path)
                        raise DemoError('world_api_unexpected_http_' + str(response.status_code) + ' at ' + step)
                    return response
                registered = (await call('POST', '/v1/agents', expected=(201,), json={
                    'name': 'External world agent', 'capabilities': ['wallet.read', 'payment.prepare',
                        'owner.request_decision', 'payment.execute_sandbox']})).json()
                agent_headers = {'Authorization': 'Bearer ' + registered['api_key']}
                await call('POST', '/v1/wallets', expected=(201,), json={
                    'agent_id': registered['agent']['agent_id'], 'currency': 'USD',
                    'monthly_budget': '2.00', 'per_transaction_limit': '1.00'})
                async def prepare(name, amount='1.00'):
                    return (await call('POST', '/v1/payment-requests', expected=(201,),
                        headers={**agent_headers, 'Idempotency-Key': 'world-' + uuid.uuid4().hex},
                        json={'amount': amount, 'currency': 'USD', 'merchant_name': name,
                              # Approval-bound payee (P1): PayPal must pay THIS merchant account.
                              **({'merchant_paypal_email': payee_of[name]} if payee_of.get(name) else {}),
                              'merchant_domain': ''.join(c for c in name.lower() if c.isalpha()) + '.example', 'purpose': 'Sandbox scenario; no real money'})).json()
                async def approve_execute(payment):
                    path = '/v1/payment-requests/' + payment['request_id']
                    await call('POST', path + '/approve', json={'terms_sha256': payment['terms_sha256']})
                    await call('POST', path + '/execute', headers=agent_headers)
                    refs = (await call('GET', path + '/reconciliation')).json()['provider_references']
                    if len(refs) != 1:
                        raise DemoError('world_provider_reference_missing')
                    return path, refs[0]
                # a: independent authenticated OWNER approval, sandbox BUYER, verified capture.
                normal = await prepare(normal_name)
                path, ref = await approve_execute(normal)
                if backend:
                    backend.approve(ref['provider_order_id'])
                else:
                    if buyer is None:
                        from .sandbox_buyer import approve_sandbox_buyer
                        buyer = approve_sandbox_buyer
                    result = await asyncio.to_thread(buyer, ref['payer_action_url'], report_dir=report_path)
                    if result.status != 'approved':
                        raise DemoError('sandbox_buyer_' + result.reason)
                # PayPal may need a few seconds to register the buyer's click. The capture
                # route reads the order first and refuses (409) WITHOUT a capture POST while it
                # is unapproved, so a short bounded retry can never capture twice.
                for _attempt in range(6):
                    response = await call('POST', path + '/capture', expected=(200, 409))
                    if response.status_code == 200 or backend:
                        break
                    await asyncio.sleep(3)
                captured = response.json()
                if captured['status'] != 'provider_confirmed':
                    raise DemoError('normal_payment_not_verified')
                scenarios.append({'scenario': 'normal_purchase', 'state': captured['status'],
                    'order_id': redacted_id(ref['provider_order_id']), 'amount': '1.00', 'currency': 'USD'})
                # d: replays must never create/capture twice (a GET reconciliation is safe).
                execute_replay = await call('POST', path + '/execute', headers=agent_headers, expected=(200, 409))
                capture_replay = await call('POST', path + '/capture', expected=(200, 409))
                if calls['create'] != 1 or calls['capture'] != 1:
                    raise DemoError('duplicate_provider_mutation')
                scenarios.append({'scenario': 'duplicates', 'state': 'provider_confirmed',
                    'execute_http_status': execute_replay.status_code,
                    'capture_http_status': capture_replay.status_code, 'capture_posts': calls['capture']})
                # b: owner rejection creates no provider object.
                risky = await prepare(risky_name, '0.50')
                risky_path = '/v1/payment-requests/' + risky['request_id']
                rejected = (await call('POST', risky_path + '/reject')).json()
                await call('POST', risky_path + '/execute', headers=agent_headers, expected=(409,))
                refs = (await call('GET', risky_path + '/reconciliation')).json()['provider_references']
                if refs or calls['create'] != 1 or rejected['status'] != 'rejected':
                    raise DemoError('rejected_request_reached_provider')
                scenarios.append({'scenario': 'owner_rejects_risky', 'state': rejected['status'], 'provider_orders': 0})
                # c: no BUYER approval; the real provider read must refuse capture.
                pending = await prepare(next(p['name'] for p in personas if p['name'] == 'Bluepeak Electronics'))
                pending_path, pending_ref = await approve_execute(pending)
                refused = await call('POST', pending_path + '/capture', expected=(409,))
                pending_state = (await call('GET', pending_path)).json()['status']
                if pending_state != 'provider_pending' or calls['capture'] != 1:
                    raise DemoError('unapproved_buyer_captured')
                scenarios.append({'scenario': 'buyer_never_approves', 'state': pending_state,
                    'capture_http_status': refused.status_code, 'order_id': redacted_id(pending_ref['provider_order_id'])})
                # e: forge an actual known order reference; unsigned bytes cannot settle it.
                forged = await call('POST', '/v1/providers/paypal/webhooks', headers={}, expected=(400,), json={
                    'id': 'FORGED-WORLD-EVENT', 'event_type': 'PAYMENT.CAPTURE.COMPLETED',
                    'resource': {'id': 'FORGED-CAPTURE', 'status': 'COMPLETED',
                        'amount': {'value': '1.00', 'currency_code': 'USD'},
                        'supplementary_data': {'related_ids': {'order_id': pending_ref['provider_order_id']}}}})
                after = (await call('GET', pending_path)).json()['status']
                if after != pending_state:
                    raise DemoError('forged_event_changed_state')
                scenarios.append({'scenario': 'forged_webhook', 'http_status': forged.status_code, 'state': after})
                # f: monthly allowance is fully consumed/held; policy blocks before PayPal.
                blocked = await prepare(normal_name, '1.00')
                if blocked['status'] != 'rejected' or calls['create'] != 2:
                    raise DemoError('over_budget_reached_provider')
                scenarios.append({'scenario': 'over_budget', 'state': blocked['status'], 'provider_orders': 0})
                # g: owner-only, read-only PayPal report; redact references before persisting.
                reconciliation = (await call('GET', '/v1/paypal/reconciliation', params={
                    'start': (run_start - timedelta(minutes=1)).isoformat(),
                    'end': datetime.now(timezone.utc).isoformat()})).json()
                for row in reconciliation['rows']:
                    row['request_id'] = redacted_id(row['request_id'])
                    if row['provider_order_id']:
                        row['provider_order_id'] = redacted_id(row['provider_order_id'])
                outcomes = deps.risk_service.store.query('SELECT * FROM payment_outcomes')
                if len(outcomes) != 1:
                    raise DemoError('unexpected_verified_outcome_count')
                report = {'mode': 'live_sandbox' if live else 'offline_mocked_paypal',
                    'is_real_money': False, 'started_at': run_start.isoformat(),
                    'elapsed_seconds': round(time.monotonic() - started, 3), 'scenarios': scenarios,
                    'provider_calls': dict(calls), 'verified_outcomes': len(outcomes),
                    'capture_ids': [redacted_id(row['provider_reference']) for row in outcomes
                                    if isinstance(row.get('provider_reference'), str)],
                    'reconciliation': reconciliation,
                    'live_webhooks': 'UNVERIFIED: requires a registered public HTTPS listener',
                    'payee_routing': 'Approval-bound merchant payee from personas (P1); verified on capture'}
                if report_path:
                    (report_path / 'paypal-world.json').write_text(json.dumps(report, indent=2) + '\n')
                return report
        finally:
            deps.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run_world(live=args.live, report_dir=args.report)), indent=2))
        return 0
    except Exception as exc:
        # Only our constant DemoError codes may be shown; third-party errors stay private.
        code = str(exc) if isinstance(exc, DemoError) else type(exc).__name__
        print(json.dumps({'status': 'BLOCKED', 'error': code, 'is_real_money': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
