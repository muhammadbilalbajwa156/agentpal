"""Deterministic documentation and secret-free auth contracts for WS3."""

from __future__ import annotations

import copy
import json
import uuid

from postman.generate import (
    AUTH_VARIABLES,
    build_collection,
    build_environment,
    generate,
    legacy_spec,
    serialize,
)


def operations(spec):
    return {(method.upper(), path) for path, methods in spec['paths'].items()
            for method in methods if method in {'get', 'post', 'put', 'patch', 'delete',
                                                'head', 'options', 'trace'}}


def test_every_openapi_operation_once_and_deterministic():
    spec = legacy_spec()
    first = build_collection(spec)
    reordered = json.loads(json.dumps(spec, sort_keys=True))
    assert serialize(first) == serialize(build_collection(reordered))
    items = first['item'][0]['item']
    actual = [(item['request']['method'], item['request']['url']['raw']
               .removeprefix('{{base_url}}')) for item in items]
    expected = {(method, path.replace('{request_id}', '{{request_id}}'))
                for method, path in operations(spec)}
    assert len(actual) == len(expected) and set(actual) == expected
    assert uuid.UUID(first['info']['_postman_id']).version == 5
    assert all(uuid.UUID(item['id']).version == 5 for item in items)
    assert first['info']['schema'].endswith('/v2.1.0/collection.json')
    assert 'timestamp' not in serialize(first)


def test_auth_uses_only_empty_secret_collection_variables():
    collection = build_collection(legacy_spec())
    variables = {v['key']: v for v in collection['variable']}
    for key in AUTH_VARIABLES:
        assert variables[key] == {'key': key, 'value': '', 'type': 'string'}  # Postman API contract
    assert collection['auth']['bearer'][0]['value'] == '{{agent_a_key}}'
    for folder in collection['item']:
        for item in folder['item']:
            auth = item['request'].get('auth', {})
            for field in auth.get('bearer', []):
                assert field['value'] in {'{{' + name + '}}' for name in AUTH_VARIABLES}
    assert not ({v['key'] for v in build_environment()['values']} & set(AUTH_VARIABLES))
    for event in collection['event']:
        assert 'pm.test(' in serialize(event)


def test_url_objects_include_sdk_components_for_all_requests():
    # Postman's SDK ignores a URL object containing only `raw`; it needs host/path.
    for mode, spec in [('legacy', legacy_spec()), ('v1', None)]:
        for folder in build_collection(spec, mode)['item']:
            for item in folder['item']:
                url = item['request']['url']
                assert url['host'] == ['{{base_url}}']
                assert url['raw'].split('?')[0] == '{{base_url}}/' + '/'.join(url['path'])


def test_generator_check_detects_drift_without_writing(tmp_path):
    missing = tmp_path / 'no-spec.json'
    assert generate(tmp_path, spec_path=missing) == []
    before = {p.name: p.read_bytes() for p in tmp_path.glob('*.json')}
    assert generate(tmp_path, check=True, spec_path=missing) == []
    target = tmp_path / 'legacy.collection.json'
    target.write_text('{}\n')
    assert generate(tmp_path, check=True, spec_path=missing) == [target.name]
    assert target.read_text() == '{}\n'
    assert before['v1.collection.json'] == (tmp_path / 'v1.collection.json').read_bytes()


def test_checked_in_artifacts_have_no_drift():
    assert generate(check=True) == []


def test_v1_pending_uses_no_external_spec_and_has_real_draft_scenarios(tmp_path):
    generate(tmp_path, spec_path=tmp_path / 'missing.json')
    collection = json.loads((tmp_path / 'v1.collection.json').read_text())
    folder = next(f for f in collection['item'] if f['name'] == 'PENDING v1')
    assert len(folder['item']) >= 12
    text = serialize(folder)
    for term in ['terms_sha256', 'is_real_money', 'Idempotency-Key',
                 '/approve', '/execute', '{{owner_key}}', '{{agent_a_key}}']:
        assert term in text
    assert all('pm.test(' in serialize(item['event']) for item in folder['item'])


def test_v1_local_spec_generates_each_operation_and_enables_folder(tmp_path):
    spec = {'openapi': '3.1.0', 'info': {'title': 'fixture', 'version': '1'}, 'paths': {
        '/v1/me': {'get': {'security': [{'AgentKey': []}], 'responses': {'200': {}}}},
        '/v1/agents': {'post': {'security': [{'OwnerToken': []}], 'responses': {'201': {}}}},
    }}
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(spec))
    generate(tmp_path, spec_path=path)
    generated = json.loads((tmp_path / 'v1.collection.json').read_text())
    assert generated['item'][1]['name'] == 'V1 security'
    assert len(generated['item'][0]['item']) == 2
    owner = next(i for i in generated['item'][0]['item'] if i['request']['method'] == 'POST')
    assert owner['request']['auth']['bearer'][0]['value'] == '{{owner_key}}'


def test_parameter_and_body_examples_resolve_local_refs_without_mutating_spec():
    spec = {'paths': {'/items/{item_id}': {'parameters': [
        {'name': 'item_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
        'post': {'parameters': [
            {'name': 'limit', 'in': 'query', 'required': True, 'schema': {'default': 3}},
            {'name': 'Authorization', 'in': 'header', 'schema': {'example': 'must-not-copy'}},
        ], 'requestBody': {'content': {'application/json': {'schema': {
            '$ref': '#/components/schemas/Input'}}}}, 'responses': {'200': {}}}}},
        'components': {'schemas': {'Input': {'type': 'object', 'required': ['amount'],
            'properties': {'amount': {'type': 'string', 'examples': ['1.00']}}}}}}
    original = copy.deepcopy(spec)
    item = build_collection(spec)['item'][0]['item'][0]['request']
    assert item['url']['raw'] == '{{base_url}}/items/{{item_id}}?limit=3'
    assert json.loads(item['body']['raw']) == {'amount': '1.00'}
    assert 'must-not-copy' not in serialize(item)
    assert spec == original
