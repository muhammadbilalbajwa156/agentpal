"""Hand-authored Postman assertions; generated API examples do not replace these tests.

The runner provisions real owner credentials, agents, USD wallets and sandbox grants.
When the local v1 spec is absent, the generated folder remains a pending draft.
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

AUTH_VARIABLES = ('agent_a_key', 'agent_b_key', 'owner_key', 'invalid_key')


def stable_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, 'https://agentpal.local/postman/' + name))


def bearer(variable: str) -> dict:
    return {'type': 'bearer', 'bearer': [
        {'key': 'token', 'value': '{{' + variable + '}}', 'type': 'string'}]}


def script(source: str, listen: str = 'test') -> dict:
    return {'listen': listen, 'script': {'type': 'text/javascript', 'exec': source.splitlines()}}


def scenario(name, method, path, assertions, *, auth='agent_a_key', body=None,
             headers=None, prerequest=None):
    request = {'method': method, 'url': {'raw': '{{base_url}}' + path,
               'host': ['{{base_url}}'], 'path': path.lstrip('/').split('/')},
               'auth': bearer(auth) if auth else {'type': 'noauth'}, 'header': headers or []}
    if body is not None:
        request['body'] = {'mode': 'raw', 'raw': json.dumps(body, sort_keys=True, indent=2),
                           'options': {'raw': {'language': 'json'}}}
        request['header'].append({'key': 'Content-Type', 'value': 'application/json'})
    events = [script(assertions)]
    if prerequest:
        events.insert(0, script(prerequest, 'prerequest'))
    return {'id': stable_id('scenario/' + name), 'name': name,
            'request': request, 'event': events}


def error_assertions(status: int, error: str | None = None) -> str:
    body_check = (f"pm.expect(pm.response.json()).to.deep.equal({{error: '{error}'}});"
                  if error else "pm.expect(pm.response.json().error).to.be.a('string').and.not.empty;\n"
                  "  pm.expect(pm.response.json()).not.to.have.property('decision');")
    return f"""pm.test('refused with safe JSON error', function () {{
  pm.response.to.have.status({status});
  {body_check}
}});"""


LEAK_ASSERTIONS = """pm.test('error body never echoes credentials', function () {
  if (pm.response.code >= 400) {
    ['agent_a_key', 'agent_b_key', 'owner_key', 'invalid_key'].forEach(function (key) {
      const value = pm.collectionVariables.get(key);
      if (value) { pm.expect(pm.response.text()).not.to.include(value); }
    });
  }
});"""


def legacy_folder() -> dict:
    body = {'request_id': 'req_postman_001', 'agent_id': '{{agent_a_id}}',
            'amount': '1.00', 'currency': 'USD', 'merchant_name': 'Example Hosting',
            'merchant_domain': 'example.com', 'purpose': 'Postman local security scenario',
            'business_justification': 'Throwaway API verification', 'evidence': []}
    challenge = """
pm.test('bearer challenge is present', function () {
  pm.expect(pm.response.headers.get('WWW-Authenticate')).to.equal('Bearer realm="agentpal"');
});"""
    items = [
        scenario('Health disables execution', 'GET', '/health', """pm.test('assessment only, authentication required', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.status).to.equal('ok');
  pm.expect(body.payment_execution_enabled).to.equal(false);
  pm.expect(body.authentication).to.equal('required');
});""", auth=None),
        scenario('Missing authentication', 'GET', '/model-info',
                 error_assertions(401, 'authentication_required') + challenge, auth=None),
        scenario('Invalid authentication', 'GET', '/model-info',
                 error_assertions(401, 'invalid_token') + challenge, auth='invalid_key'),
        scenario('Create owned assessment', 'POST', '/assess', """pm.test('assessment belongs to authenticated agent', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.request_id).to.equal('req_postman_001');
  pm.expect(body.agent_id).to.equal(pm.collectionVariables.get('agent_a_id'));
  pm.expect(body.recommendation).not.to.equal('block');
  pm.expect(body.signals).to.be.an('array');
});""", body=body),
        scenario('Read own assessment', 'GET', '/assessments/req_postman_001', """pm.test('owner reads the stored assessment', function () {
  pm.response.to.have.status(200);
  pm.expect(pm.response.json().request_id).to.equal('req_postman_001');
  pm.expect(pm.response.json().agent_id).to.equal(pm.collectionVariables.get('agent_a_id'));
});"""),
        scenario('Unknown assessment', 'GET', '/assessments/req_postman_unknown',
                 error_assertions(404, 'not_found') + "\npm.collectionVariables.set('unknown_error', pm.response.text());",
                 auth='agent_b_key'),
        scenario('Cross agent assessment indistinguishable', 'GET', '/assessments/req_postman_001',
                 error_assertions(404, 'not_found') + """
pm.test('no assessment existence oracle', function () {
  pm.expect(pm.response.text()).to.equal(pm.collectionVariables.get('unknown_error'));
});""", auth='agent_b_key'),
        scenario('Agent id mismatch', 'POST', '/assess', error_assertions(403, 'agent_mismatch'),
                 body={**body, 'request_id': 'req_postman_mismatch', 'agent_id': '{{agent_b_id}}'}),
        scenario('Request id replay blocks', 'POST', '/assess', """pm.test('replay cannot reserve or pass again', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.request_id).to.equal('req_postman_001');
  pm.expect(body.recommendation).to.equal('block');
  pm.expect(body.signals.some(function (signal) {
    return signal.code === 'DUPLICATE_REQUEST_ID' && signal.source === 'rule';
  })).to.equal(true);
});""", body=body),
    ]
    return {'id': stable_id('folder/legacy-security'), 'name': 'LEGACY security', 'item': items}


def v1_folder(pending=True) -> dict:
    payment = {'amount': '1.00', 'currency': 'USD', 'merchant_name': 'Example Hosting',
               'merchant_domain': 'example.com', 'purpose': 'Postman sandbox verification',
               'request_owner_decision': True}
    idem = [{'key': 'Idempotency-Key', 'value': '{{v1_idempotency}}'}]
    create_assertions = """pm.test('prepared request requires exact owner approval', function () {
  pm.response.to.have.status(201);
  const body = pm.response.json();
  pm.expect(body.is_real_money).to.equal(false);
  pm.expect(body.requires_owner_approval).to.equal(true);
  pm.expect(body.terms_sha256).to.match(/^[0-9a-f]{64}$/);
  pm.expect(body.status).to.equal('awaiting_approval');
  pm.collectionVariables.set('v1_request_id', body.request_id);
  pm.collectionVariables.set('v1_terms_sha256', body.terms_sha256);
  pm.collectionVariables.set('v1_created_response', JSON.stringify(body));
});"""
    path = '/v1/payment-requests/{{v1_request_id}}'
    items = [
        scenario('V1 owner cannot act as agent', 'GET', '/v1/me', error_assertions(403), auth='owner_key'),
        scenario('V1 agent cannot act as owner', 'GET', '/v1/agents', error_assertions(403)),
        scenario('V1 virtual allowance is not money', 'GET', '/v1/me/wallet', """pm.test('virtual ledger-derived allowance', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.is_real_money).to.equal(false);
  pm.expect(body.unit).to.equal('virtual_allowance');
  pm.expect(body.wallets).to.be.an('array').that.is.not.empty;
  body.wallets.forEach(function (wallet) {
    pm.expect(wallet.is_real_money).to.equal(false);
    pm.expect(wallet.unit).to.equal('virtual_allowance');
    ['available', 'reserved', 'spent', 'committed'].forEach(function (field) {
      pm.expect(wallet.allowance[field]).to.match(/^[0-9]+(?:\\.[0-9]+)?$/);
    });
  });
});"""),
        scenario('V1 create idempotent request', 'POST', '/v1/payment-requests', create_assertions,
                 body=payment, headers=[*idem], prerequest="pm.collectionVariables.set('v1_idempotency', pm.variables.replaceIn('postman-{{$guid}}'));"),
        scenario('V1 idempotency exact replay', 'POST', '/v1/payment-requests', """pm.test('replay returns the original response', function () {
  pm.response.to.have.status(200);  // documented: first create 201, exact replay 200
  pm.expect(pm.response.json()).to.deep.equal(JSON.parse(pm.collectionVariables.get('v1_created_response')));
});""", body=payment, headers=[*idem]),
        scenario('V1 idempotency body mismatch', 'POST', '/v1/payment-requests', error_assertions(409),
                 body={**payment, 'amount': '2.00'}, headers=[*idem]),
        scenario('V1 execute before approval refused', 'POST', path + '/execute', error_assertions(409)),
        scenario('V1 agent cannot approve itself', 'POST', path + '/approve', error_assertions(403),
                 body={'terms_sha256': '{{v1_terms_sha256}}'}),
        scenario('V1 tampered approval terms refused', 'POST', path + '/approve', error_assertions(409),
                 auth='owner_key', body={'terms_sha256': '0' * 64}),
        scenario('V1 refusal preserves approval gate', 'GET', path, """pm.test('no refusal bypassed the owner gate', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.status).to.equal('awaiting_approval');
  pm.expect(body.terms_sha256).to.equal(pm.collectionVariables.get('v1_terms_sha256'));
  pm.expect(body.requires_owner_approval).to.equal(true);
});"""),
        scenario('V1 exact terms owner approval', 'POST', path + '/approve', """pm.test('owner approval binds this request', function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.request_id).to.equal(pm.collectionVariables.get('v1_request_id'));
  pm.expect(body.decision).to.equal('approved');
  pm.expect(body.status).to.equal('approved');
});""", auth='owner_key', body={'terms_sha256': '{{v1_terms_sha256}}'}),
        scenario('V1 double approval refused', 'POST', path + '/approve', error_assertions(409),
                 auth='owner_key', body={'terms_sha256': '{{v1_terms_sha256}}'}),
        scenario('V1 sandbox execute remains pending', 'POST', path + '/execute', """pm.test('simulator cannot claim completion', function () {
  pm.response.to.have.status(200);
  pm.expect(pm.response.json().status).to.equal('provider_pending');
});"""),
        scenario('V1 execution approval is single use', 'POST', path + '/execute',
                 error_assertions(409)),
        scenario('V1 other agent cannot read request', 'GET', path,
                 error_assertions(404), auth='agent_b_key'),
        scenario('V1 unsigned webhook refused', 'POST', '/v1/providers/paypal/webhooks',
                 error_assertions(400), auth=None, body={'verified': True, 'status': 'completed'}),
        scenario('V1 status stays pending', 'GET', path, """pm.test('only verified provider events can complete', function () {
  pm.response.to.have.status(200);
  pm.expect(pm.response.json().status).to.equal('provider_pending');
});"""),
    ]
    return {'id': stable_id('folder/v1-security'), 'name': 'PENDING v1' if pending else 'V1 security',
            'description': ('DESIGN ONLY: local v1 spec absent.' if pending else
                'Real /v1 security scenarios. run_newman provisions owner identity, agents, virtual wallets and sandbox grants against serve --platform. Local simulator never completes payments. Never uses real money.'),
            'item': items}
