"""Bounded malformed JSON, streaming bodies and safe errors (no external I/O)."""
import asyncio
import socket
import time

import pytest

from .conftest import PORT, legacy_body, raw_request


@pytest.mark.parametrize('raw', [b'{', b'[]', b'null', b'true', b'"text"',
    b'{"name": NaN}', b'{"name": Infinity}', b'{"name": -Infinity}',
    b'{"name":1e999999}', b'{"name":' + b'9' * 5000 + b'}',
    b'{"name":"\\ud800"}', b'{"name":"\\udfff"}', b'{"name":"\xff"}',
    b'{"name":"first","name":false}', b'{"name":"qa", "metadata":{"x":NaN}}',
    b'{"name":"qa", "metadata":' + b'[' * 100 + b'0' + b']' * 100 + b'}'])
def test_malformed_json_is_bounded_rejection_not_a_crash(edge, raw):
    response = edge.client.post('/v1/agents', content=raw,
        headers={'Authorization': 'Bearer ' + edge.owner, 'Content-Type': 'application/json'})
    assert response.status_code in {400, 422}
    assert response.json()['error'] in {'invalid_request', 'http_error'}
    assert 'Traceback' not in response.text
    assert edge.client.get('/health').status_code == 200


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity', '1e999999',
                                    '9' * 5000, 'true', 'null', '"1e1000000"'])
def test_legacy_numeric_abuse_is_rejected(edge, number):
    raw = legacy_body().replace(b'"amount": "1.00"', b'"amount": ' + number.encode())
    response = edge.client.post('/assess', content=raw,
        headers={'Authorization': 'Bearer ' + edge.legacy, 'Content-Type': 'application/json'})
    assert response.status_code in {400, 422}
    assert response.json()['error'] in {'invalid_request', 'http_error'}


@pytest.mark.parametrize('content_type', ['text/plain', 'application/octet-stream',
    'application/x-www-form-urlencoded', 'multipart/form-data; boundary=h1'])
@pytest.mark.parametrize('path', ['/v1/agents', '/assess'])
def test_content_type_mismatches_cannot_be_interpreted_as_json(edge, content_type, path):
    credential = edge.owner if path == '/v1/agents' else edge.legacy
    raw = b'{"name":"H1 mismatch"}' if path == '/v1/agents' else legacy_body()
    response = edge.client.post(path, content=raw, headers={
        'Authorization': 'Bearer ' + credential, 'Content-Type': content_type})
    assert response.status_code == 422


@pytest.mark.parametrize('path', ['/v1/agents', '/assess'])
@pytest.mark.parametrize('framing', ['declared', 'chunked'])
def test_default_body_cap_applies_without_credentials_on_real_tcp(tcp, path, framing):
    if framing == 'declared':
        status, data = raw_request(target=path.encode(), method=b'POST',
                                  headers=[(b'Content-Length', b'32769')])
    else:
        # Valid HTTP chunk framing; never buffer more than 40 KiB in the test.
        raw = b'x' * 32769
        chunked = f'{len(raw):x}\r\n'.encode() + raw + b'\r\n0\r\n\r\n'
        status, data = raw_request(target=path.encode(), method=b'POST',
                                  headers=[(b'Transfer-Encoding', b'chunked')], body=chunked)
    assert status == 413
    assert b'request_too_large' in data
    assert tcp.client.get('/health').status_code == 200


@pytest.mark.parametrize('value', [b'-1', b'abc', b'+1', b'1, 2', b'1\x00'])
def test_invalid_content_length_does_not_crash_listener(tcp, value):
    status, _ = raw_request(target=b'/assess', method=b'POST',
                            headers=[(b'Content-Length', value)])
    assert status == 400
    assert tcp.client.get('/health').status_code == 200


def test_conflicting_content_lengths_are_refused_before_dispatch(tcp):
    status, _ = raw_request(target=b'/assess', method=b'POST',
        headers=[(b'Content-Length', b'2'), (b'Content-Length', b'3')], body=b'{}')
    assert status == 400


def test_identical_content_length_list_is_normalized_without_auth_bypass(tcp):
    # h11 legitimately coalesces identical repeated lengths; supply the promised byte.
    status, _ = raw_request(target=b'/assess', method=b'POST',
                            headers=[(b'Content-Length', b'1, 1')], body=b'0')
    assert status == 401


def test_streaming_limit_and_disconnect_need_no_json_parser(edge):
    async def probe(chunks):
        messages, reads = [], [0]
        async def receive():
            reads[0] += 1
            return chunks.pop(0)
        async def send(message):
            messages.append(message)
        await edge.client.app({'type': 'http', 'asgi': {'version': '3.0'},
            'method': 'POST', 'scheme': 'http', 'http_version': '1.1', 'path': '/assess',
            'raw_path': b'/assess', 'query_string': b'', 'root_path': '',
            'client': ('127.0.0.1', 49010), 'server': ('127.0.0.1', PORT),
            'headers': [(b'host', b'127.0.0.1')]}, receive, send)
        return messages, reads[0]
    messages, reads = asyncio.run(probe([
        {'type': 'http.request', 'body': b'x' * 16384, 'more_body': True},
        {'type': 'http.request', 'body': b'x' * 16385, 'more_body': True},
        {'type': 'http.request', 'body': b'never read', 'more_body': False}]))
    assert messages[0]['status'] == 413
    assert reads == 2
    messages, reads = asyncio.run(probe([{'type': 'http.disconnect'}]))
    assert messages == [] and reads == 1


# Fixed by the lead 2026-11-05 (was strict xfail E2E-H1-1).
@pytest.mark.parametrize('path,credential,raw', [
    ('/v1/agents', 'owner', b'{"name":"H1 nested","metadata":' + b'[' * 600 + b'0' + b']' * 600 + b'}'),
    ('/v1/payment-requests', 'agent', b'{"amount":"1.00","extra":' + b'[' * 600 + b'0' + b']' * 600 + b'}'),
])
def test_deep_json_returns_client_error_instead_of_recursion_500(edge, path, credential, raw):
    response = edge.client.post(path, content=raw, headers={
        'Authorization': 'Bearer ' + getattr(edge, credential), 'Content-Type': 'application/json',
        'Idempotency-Key': 'h1-deep-nesting'})
    # Health check proves the finding is a request crash, not process termination.
    assert edge.client.get('/health').status_code == 200
    assert response.status_code in {400, 422}


# Fixed by the lead 2026-11-05 (was strict xfail E2E-H1-1).
def test_deep_json_real_cli_listener_returns_client_error(tcp):
    response = tcp.client.post('/v1/agents', content=b'{"name":"H1 nested","metadata":' +
        b'[' * 600 + b'0' + b']' * 600 + b'}', headers={
            'Authorization': 'Bearer ' + tcp.owner, 'Content-Type': 'application/json'})
    assert tcp.process.poll() is None
    assert tcp.client.get('/health').status_code == 200
    assert response.status_code in {400, 422}


@pytest.mark.parametrize('path', ['/v1/agents', '/assess'])
def test_deep_json_cannot_reach_validation_without_credentials(edge, path):
    response = edge.client.post(path, content=b'[' * 600 + b'0' + b']' * 600,
                               headers={'Content-Type': 'application/json'})
    assert response.status_code == 401


# Fixed by the lead 2026-11-05 (was strict xfail E2E-H1-2).
@pytest.mark.parametrize('path', ['/v1/agents', '/assess'])
def test_incomplete_unauthenticated_body_has_a_bounded_read_deadline(tcp, path):
    # One connection, one byte, 6.5 seconds: bounded local evidence only. Uvicorn's
    # default 5-second keepalive timer does not bound an unfinished request body.
    with socket.create_connection(('127.0.0.1', PORT), timeout=2) as connection:
        connection.sendall(b'POST ' + path.encode() + b' HTTP/1.1\r\n'
            b'Host: 127.0.0.1:8201\r\nContent-Type: application/json\r\n'
            b'Content-Length: 2\r\nConnection: close\r\n\r\n{')
        connection.settimeout(6.5)
        started = time.monotonic()
        try:
            data = connection.recv(4096)
        except socket.timeout:
            data = None
        elapsed = time.monotonic() - started
        assert tcp.client.get('/health').status_code == 200
        assert data is not None, f'no close or rejection after {elapsed:.1f}s of body inactivity'
        assert not data or int(data.split(b' ', 2)[1]) in {400, 401, 408}


def test_error_docs_and_logs_do_not_echo_synthetic_secrets_or_inputs(tcp, monkeypatch):
    marker = 'h1-private-sentinel-do-not-reflect'
    fake_secret = 'h1-environment-secret-sentinel'
    monkeypatch.setenv('H1_TEST_API_KEY', fake_secret)
    response = tcp.client.post('/assess', content=legacy_body(merchant_name=marker,
        purpose=fake_secret, **{marker: marker}), headers={
            'Authorization': 'Bearer ' + tcp.legacy, 'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert marker not in response.text
    for path in ['/docs', '/openapi.json', '/redoc']:
        result = tcp.client.get(path)
        assert result.status_code == 200  # documented development-only public schema
        for value in [tcp.owner, tcp.legacy, marker, fake_secret]:
            assert value not in result.text
        assert '/tmp/m8-h1/' not in result.text
    response = tcp.client.get('/v1/agents', headers={'Authorization': 'Bearer ' + tcp.owner},
                               params={'secret': marker})
    assert response.status_code == 200
    log = tcp.logs.read_text()
    for value in [tcp.owner, tcp.legacy, marker, fake_secret]:
        assert value not in log
    for database in tcp.directory.glob('*.db'):
        raw = database.read_bytes()
        assert tcp.owner.encode() not in raw
        assert tcp.legacy.encode() not in raw


def test_sql_template_script_and_log_payloads_stay_json_data(edge):
    payload = "H1 '; DROP TABLE owners;-- {{7*7}} <script>alert(1)</script>\nforged=approved"
    response = edge.client.post('/v1/agents', json={'name': 'H1 inert metadata',
        'description': payload}, headers={'Authorization': 'Bearer ' + edge.owner})
    assert response.status_code == 201
    assert response.headers['content-type'].startswith('application/json')
    assert response.json()['agent']['description'] == payload
    assert edge.client.get('/v1/agents',
        headers={'Authorization': 'Bearer ' + edge.owner}).status_code == 200
    assert edge.client.get('/health').status_code == 200
