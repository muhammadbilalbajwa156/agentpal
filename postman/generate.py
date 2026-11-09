"""Deterministic OpenAPI -> Postman v2.1; run python -m postman.generate [--check]."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from .scenarios import AUTH_VARIABLES, LEAK_ASSERTIONS, bearer, legacy_folder, script, stable_id, v1_folder

ROOT = Path(__file__).resolve().parent
V1_SPEC = ROOT.parent / 'docs' / 'api' / 'openapi.json'
METHODS = frozenset({'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'})
AUTH_HEADERS = frozenset({'authorization', 'proxy-authorization', 'x-api-key'})


def serialize(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def legacy_spec() -> dict:
    """Read app schema without entering lifespan, opening a DB or loading tokens."""
    from agentpal.api import create_app
    from agentpal.auth import TokenRegistry
    from agentpal.config import Settings

    return create_app(settings=Settings(environment='development', auth_mode='required',
                                       api_tokens_file=None),
                      registry=TokenRegistry(tokens=[])).openapi()


def resolve(schema: dict, spec: dict) -> dict:
    seen = set()
    while '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/') or ref in seen:
            raise ValueError('Only acyclic local OpenAPI references are supported')
        seen.add(ref)
        target = spec
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        schema = {**target, **{k: v for k, v in schema.items() if k != '$ref'}}
    return schema


def example(schema: dict, spec: dict, depth: int = 0):
    if depth > 12:
        return None
    schema = resolve(schema, spec)
    for field in ('example', 'const', 'default'):
        if field in schema:
            return schema[field]
    if schema.get('examples'):
        return schema['examples'][0]
    if schema.get('enum'):
        return schema['enum'][0]
    for field in ('oneOf', 'anyOf', 'allOf'):
        if schema.get(field):
            return example(schema[field][0], spec, depth + 1)
    kind = schema.get('type')
    if kind == 'object' or 'properties' in schema:
        return {name: example(schema.get('properties', {}).get(name, {}), spec, depth + 1)
                for name in sorted(schema.get('required', []))}
    if kind == 'array':
        return []
    if kind in {'integer', 'number'}:
        return schema.get('minimum', 1)
    if kind == 'boolean':
        return False
    if kind == 'null':
        return None
    return 'example'


def operation_auth(operation: dict, spec: dict, path: str) -> dict:
    if path in {'/health', '/v1/health'}:
        return {'type': 'noauth'}
    security = operation.get('security', spec.get('security'))
    if security == []:
        return {'type': 'noauth'}
    names = {name.lower() for requirement in security or [] for name in requirement}
    return bearer('owner_key' if any('owner' in name for name in names) else 'agent_a_key')


def operation_item(method: str, path: str, operation: dict, path_item: dict, spec: dict) -> dict:
    url = '{{base_url}}' + re.sub(r'\{([^{}]+)\}', r'{{\1}}', path)
    headers, queries, parameters = [], [], {}
    for raw_parameter in [*path_item.get('parameters', []), *operation.get('parameters', [])]:
        parameter = resolve(raw_parameter, spec)
        parameters[(parameter['in'], parameter['name'])] = parameter
    for (location, name), parameter in sorted(parameters.items()):
        value = str(parameter.get('example', example(parameter.get('schema', {}), spec)))
        if location == 'header' and name.lower() not in AUTH_HEADERS:
            headers.append({'key': name, 'value': value})
        if location == 'query':
            queries.append({'key': name, 'value': value,
                            'disabled': not parameter.get('required', False)})
    enabled_queries = [(p['key'], p['value']) for p in queries if not p['disabled']]
    if enabled_queries:
        url += '?' + urlencode(enabled_queries)
    request = {'method': method.upper(), 'url': {'raw': url, 'host': ['{{base_url}}'],
               'path': re.sub(r'\{([^{}]+)\}', r'{{\1}}', path).lstrip('/').split('/')},
               'header': headers,
               'auth': operation_auth(operation, spec, path),
               'description': operation.get('description', operation.get('summary', ''))}
    if queries:
        request['url']['query'] = queries
    if operation.get('requestBody'):
        content = resolve(operation['requestBody'], spec).get('content', {})
        if 'application/json' in content:
            media = content['application/json']
            payload = media.get('example', example(media.get('schema', {}), spec))
            request['body'] = {'mode': 'raw', 'raw': serialize(payload),
                               'options': {'raw': {'language': 'json'}}}
            headers.append({'key': 'Content-Type', 'value': 'application/json'})
    return {'id': stable_id('operation/' + method.upper() + ' ' + path),
            'name': method.upper() + ' ' + path, 'request': request, 'response': []}


def build_collection(spec: dict | None, mode: str = 'legacy') -> dict:
    if mode not in {'legacy', 'v1'}:
        raise ValueError('mode must be legacy or v1')
    items = []
    if spec is not None:
        for path, path_item in sorted(spec.get('paths', {}).items()):
            for method, operation in sorted(path_item.items()):
                if method in METHODS:
                    items.append(operation_item(method, path, operation, path_item, spec))
    return {
        'info': {'_postman_id': stable_id('collection/' + mode), 'name': 'AgentPal ' + mode,
                 'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
                 'description': 'IMPLEMENTED — TESTED: legacy security scenarios. V1 scenarios are DESIGN ONLY until verified with WS1/WS2 wiring. OpenAPI examples are documentation; select the security folder to run assertions. No real money.'},
        'auth': bearer('agent_a_key'), 'event': [script(LEAK_ASSERTIONS)],
        'variable': [
            {'key': 'base_url', 'value': 'http://127.0.0.1:8000', 'type': 'string'},
            {'key': 'agent_a_id', 'value': 'agent_demo_001', 'type': 'string'},
            {'key': 'agent_b_id', 'value': 'agent_demo_002', 'type': 'string'},
            {'key': 'request_id', 'value': 'req_postman_001', 'type': 'string'},
            # Postman's API rejects type 'secret' on collection variables (live 400 malformedRequestError,
            # 2026-11-01); credentials are empty 'string' variables, filled only at run time.
            *[{'key': key, 'value': '', 'type': 'string'} for key in AUTH_VARIABLES],
        ],
        'item': [
            {'id': stable_id('folder/openapi/' + mode), 'name': 'OpenAPI ' + mode,
             'description': 'Generated documentation examples. Security scenarios run separately.',
             'item': items},
            legacy_folder() if mode == 'legacy' else v1_folder(pending=spec is None),
        ],
    }


def build_environment() -> dict:
    return {'id': stable_id('environment/local'), 'name': 'AgentPal loopback',
            '_postman_variable_scope': 'environment', 'values': [
                {'key': 'base_url', 'value': 'http://127.0.0.1:8000',
                 'enabled': True, 'type': 'default'}]}


def generate(root: Path = ROOT, *, check: bool = False, spec_path: Path = V1_SPEC) -> list[str]:
    v1 = json.loads(spec_path.read_text(encoding='utf-8')) if spec_path.exists() else None
    outputs = {'legacy.collection.json': build_collection(legacy_spec()),
               'v1.collection.json': build_collection(v1, 'v1'),
               'environment.json': build_environment()}
    drift = []
    for name, value in sorted(outputs.items()):
        path, rendered = root / name, serialize(value)
        if check:
            if not path.exists() or path.read_text(encoding='utf-8') != rendered:
                drift.append(name)
        else:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding='utf-8')
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail on drift without writing')
    args = parser.parse_args(argv)
    drift = generate(check=args.check)
    if drift:
        print('DRIFT: ' + ', '.join(drift))
        return 1
    print('Postman artifacts match.' if args.check else 'Generated deterministic Postman artifacts.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
