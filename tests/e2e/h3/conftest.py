"""H3 real in-process platform, private databases and offline-only HTTP."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform
from test_provider_paypal_support import make_provider


@pytest.fixture
def h3_world():
    root = Path('/tmp/m8-h3')
    root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='inprocess-', dir=root) as directory:
        settings = Settings(db_path=str(Path(directory) / 'risk.db'), auth_mode='required',
                            analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'])
        deps = build_platform(settings=settings, environ={})
        provider, fake = make_provider()
        provider._now = deps.risk_service.clock
        deps.provider = deps.lifecycle.provider = provider
        state = SimpleNamespace(order={}, overrides={})

        def response(phase, code):
            if phase in state.overrides:
                out = state.overrides[phase]
                return out if isinstance(out, httpx.Response) else httpx.Response(code, json=out)
            return httpx.Response(code, json=deepcopy(state.order))

        def create(request):
            unit = json.loads(request.content)['purchase_units'][0]
            unit['payee'] = {'email_address': unit.get('payee', {}).get(
                'email_address', 'merchant@example.com'), 'merchant_id': 'MERCHANT-H3'}
            state.order = dict(id='ORDER-H3', status='CREATED', purchase_units=[unit])
            return response('create', 201)

        def capture(request):
            state.order['status'] = 'COMPLETED'
            state.order['purchase_units'][0]['payments'] = {'captures': [{
                'id': 'CAPTURE-H3', 'status': 'COMPLETED',
                'amount': {'value': '1.00', 'currency_code': 'USD'}}]}
            return response('capture', 201)

        fake.routes[('POST', '/v2/checkout/orders')] = create
        fake.routes[('GET', '/v2/checkout/orders/ORDER-H3')] = lambda r: response('read', 200)
        fake.routes[('POST', '/v2/checkout/orders/ORDER-H3/capture')] = capture
        identity = deps.owner_authenticator
        identity.create_owner('h3_owner')
        owner_token, _ = identity.issue_token('h3_owner')
        owner = {'Authorization': 'Bearer ' + owner_token}
        app = create_app(service=deps.risk_service, settings=settings,
                         registry=TokenRegistry(), platform=deps)
        with TestClient(app, base_url='http://127.0.0.1', raise_server_exceptions=False) as client:
            registered = client.post('/v1/agents', headers=owner, json={'name': 'H3 offline',
                'capabilities': ['wallet.read', 'payment.prepare', 'owner.request_decision',
                                 'payment.execute_sandbox', 'research.fetch']})
            assert registered.status_code == 201
            agent = registered.json()
            agent_headers = {'Authorization': 'Bearer ' + agent['api_key']}
            aid = agent['agent']['agent_id']
            wallet = client.post('/v1/wallets', headers=owner, json={'agent_id': aid,
                'currency': 'USD', 'monthly_budget': '10.00', 'per_transaction_limit': '5.00'})
            assert wallet.status_code == 201

            def prepare(**changes):
                body = dict(amount='1.00', currency='USD', merchant_name='H3 store',
                    merchant_domain='example.com', purpose='H3 offline boundary verification',
                    merchant_paypal_email='merchant@example.com')
                body.update(changes)
                r = client.post('/v1/payment-requests', headers={**agent_headers,
                    'Idempotency-Key': 'h3-prepare'}, json=body)
                assert r.status_code == 201
                return r.json()

            def approve(payment):
                r = client.post('/v1/payment-requests/' + payment['request_id'] + '/approve',
                    headers=owner, json={'terms_sha256': payment['terms_sha256']})
                assert r.status_code == 200

            def pending():
                payment = prepare()
                approve(payment)
                path = '/v1/payment-requests/' + payment['request_id']
                assert client.post(path + '/execute', headers=agent_headers).status_code == 200
                state.order['status'] = 'APPROVED'
                return payment, path

            yield SimpleNamespace(client=client, deps=deps, fake=fake, state=state,
                provider=provider, owner=owner, agent=agent_headers, aid=aid,
                prepare=prepare, approve=approve, pending=pending)
        deps.close()
