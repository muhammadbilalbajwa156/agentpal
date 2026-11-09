"""Direct TLS and exact public-webhook admission, using synthetic remote peers."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
import httpx
import pytest

from agentpal.api import create_app
from agentpal.auth import TokenEntry, TokenRegistry, hash_token
from agentpal.config import Settings, UnsafeConfiguration
from agentpal.identity import OwnerIdentityService
from agentpal.identity.service import IdentityError
from agentpal.identity.store import IdentityStore
from agentpal.identity.totp import totp
from agentpal.platform.router import WEBHOOK_PATH
from agentpal.spending import SpendPolicy

from .conftest import ROOT, edge_world, local_server


@pytest.fixture
def tls_material():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tls-', dir=ROOT) as directory:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
        certpath, keypath = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
        certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        keypath.chmod(0o600)
        yield str(certpath), str(keypath)


@pytest.fixture
def public_edge(tls_material):
    with edge_world() as world:
        class RejectingProvider:
            name = 'paypal'
            environment = 'sandbox'
            calls = []
            def verify_webhook(self, headers, raw):
                self.calls.append((headers, raw))
                raise ValueError('synthetic verification rejection')
        class TrapSink:
            calls = 0
            def apply_provider_event(self, event):
                self.calls += 1
                raise AssertionError('unverified event reached sink')
        provider, sink = RejectingProvider(), TrapSink()
        world.deps.provider, world.deps.event_sink = provider, sink
        settings = world.settings.model_copy(update={
            'public_webhook_enabled': True, 'public_webhook_hosts': ['hooks.example.test'],
            'tls_certfile': tls_material[0], 'tls_keyfile': tls_material[1]})
        app = create_app(settings=settings, service=world.deps.risk_service, platform=world.deps)
        yield app, provider, sink


def test_exact_https_post_preserves_raw_bytes_but_bearer_never_replaces_signature(public_edge):
    app, provider, sink = public_edge
    raw = b'{"id":"H1-synthetic",   "resource":{}}'
    with TestClient(app, base_url='https://hooks.example.test',
                    client=('203.0.113.8', 49030)) as remote:
        response = remote.post(WEBHOOK_PATH, content=raw,
            headers={'Authorization': 'Bearer invalid', 'PayPal-Transmission-Id': 'h1-synthetic'})
    assert response.status_code == 400
    assert response.json() == {'error': 'invalid_webhook'}
    assert provider.calls[0][1] == raw
    assert provider.calls[0][0]['paypal-transmission-id'] == 'h1-synthetic'
    assert sink.calls == 0


@pytest.mark.parametrize('method,path', [('GET', WEBHOOK_PATH), ('OPTIONS', WEBHOOK_PATH),
    ('PUT', WEBHOOK_PATH), ('POST', WEBHOOK_PATH + '/'), ('POST', WEBHOOK_PATH + '?x=1'),
    ('POST', '/v1/providers/paypal%2fwebhooks'), ('POST', '/v1/providers/paypal%252fwebhooks'),
    ('POST', '/v1/providers/./paypal/webhooks'), ('POST', '/v1/agents'), ('GET', '/docs'),
    ('GET', '/health')])
def test_remote_webhook_variants_never_read_a_body(public_edge, method, path):
    app, provider, sink = public_edge
    # Build raw ASGI scopes so neither httpx nor a URL library removes dot segments.
    async def probe():
        from urllib.parse import unquote
        raw_path, _, query = path.partition('?')
        messages = []
        async def receive():
            raise AssertionError('refused remote route read a body')
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': method, 'scheme': 'https', 'path': unquote(raw_path),
            'raw_path': raw_path.encode(), 'root_path': '', 'query_string': query.encode(),
            'headers': [(b'host', b'hooks.example.test'), (b'x-http-method-override', b'POST')],
            'server': ('127.0.0.1', 8201), 'client': ('203.0.113.8', 49031)}, receive, send)
        return messages
    messages = asyncio.run(probe())
    assert messages[0]['status'] == 403
    assert provider.calls == [] and sink.calls == 0


@pytest.mark.parametrize('scheme,host,status', [('http', 'hooks.example.test', 403),
    ('https', 'evil.example', 400), ('https', 'hooks.example.test.evil', 400)])
def test_forwarded_headers_do_not_create_https_or_allowed_host(public_edge, scheme, host, status):
    app, provider, sink = public_edge
    with TestClient(app, base_url=f'{scheme}://{host}', client=('203.0.113.8', 49032)) as remote:
        response = remote.post(WEBHOOK_PATH, content=b'{}', headers={
            'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'hooks.example.test',
            'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'proto=https;host=hooks.example.test'})
    assert response.status_code == status
    assert provider.calls == [] and sink.calls == 0


def test_public_streaming_cap_and_shared_peer_admission_precede_verifier(public_edge):
    app, provider, sink = public_edge
    with TestClient(app, base_url='https://hooks.example.test',
                    client=('203.0.113.8', 49033)) as remote:
        response = remote.post(WEBHOOK_PATH, content=iter([b'x' * 16384, b'x' * 16385]))
        assert response.status_code == 413
        for _ in range(29):  # 30 total admissions, including the oversized request
            assert remote.post(WEBHOOK_PATH, content=b'{}').status_code == 400
        response = remote.post(WEBHOOK_PATH, content=b'{}')
        assert response.status_code == 429
        assert 1 <= int(response.headers['retry-after']) <= 60
    assert len(provider.calls) == 29
    assert sink.calls == 0


def test_default_exposure_off_even_for_exact_https_webhook(edge):
    with TestClient(edge.client.app, base_url='https://127.0.0.1',
                    client=('203.0.113.8', 49034)) as remote:
        assert remote.post(WEBHOOK_PATH, content=b'{}').status_code == 403


def test_remote_root_path_remount_and_websocket_never_read_body(public_edge):
    app, provider, sink = public_edge
    async def probe(kind, root):
        messages = []
        async def receive():
            raise AssertionError('refused remote remount or websocket read input')
        async def send(message):
            messages.append(message)
        scope = {'type': kind, 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'POST', 'scheme': 'https', 'path': WEBHOOK_PATH,
            'raw_path': WEBHOOK_PATH.encode(), 'root_path': root, 'query_string': b'',
            'headers': [(b'host', b'hooks.example.test')], 'server': ('127.0.0.1', 8201),
            'client': ('203.0.113.8', 49036)}
        await app(scope, receive, send)
        return messages
    assert asyncio.run(probe('http', '/remount'))[0]['status'] == 403
    assert asyncio.run(probe('websocket', ''))[0] == {'type': 'websocket.close', 'code': 1008}
    assert provider.calls == [] and sink.calls == 0


@pytest.mark.parametrize('override', [{'tls_certfile': '/nonexistent/h1-cert'},
    {'tls_certfile': '/nonexistent/h1-cert', 'tls_keyfile': '/nonexistent/h1-key'},
    {'public_webhook_enabled': True}, {'auth_mode': 'disabled_insecure_dev'}])
def test_unsafe_public_or_tls_options_refuse_before_binding(override):
    settings = Settings(auth_mode='required', environment='development', **{
        k: v for k, v in override.items() if k != 'auth_mode'})
    if 'auth_mode' in override:
        settings = settings.model_copy(update={'auth_mode': override['auth_mode']})
    with pytest.raises(UnsafeConfiguration):
        settings.check_runtime(host='0.0.0.0')


def test_mismatched_tls_pair_and_nonexact_public_hosts_refused(tls_material):
    cert, key = tls_material
    with pytest.raises(UnsafeConfiguration):
        Settings(tls_certfile=cert, tls_keyfile=cert).check_runtime()
    for host in ['*', 'https://hooks.example.test', 'hooks.example.test:443',
                 'hooks.example.test/path']:
        with pytest.raises(UnsafeConfiguration):
            Settings(auth_mode='required', environment='development', tls_certfile=cert,
                tls_keyfile=key, public_webhook_enabled=True,
                public_webhook_hosts=[host]).check_runtime()


def test_real_cli_tls_listener_requires_verified_handshake(tls_material):
    with local_server(tls=tls_material) as server:
        assert server.client.get('/v1/agents',
            headers={'Authorization': 'Bearer ' + server.owner}).status_code == 200
        with pytest.raises(httpx.TransportError):
            httpx.get('http://127.0.0.1:8201/health', timeout=2, trust_env=False)
        with pytest.raises(httpx.TransportError):
            httpx.get('https://127.0.0.1:8201/health', timeout=2, trust_env=False)


def test_production_docs_are_absent_even_for_authenticated_owner():
    with edge_world() as world:
        # Keep the deployment's actual production startup checks enabled.
        world.deps.risk_service.spend_policy = SpendPolicy(
            policy_version='h1-production-doc-check', configuration='operator')
        settings = world.settings.model_copy(update={'environment': 'production',
                                                     'api_tokens_file': 'injected-registry'})
        registry = TokenRegistry(tokens=[TokenEntry(id='h1-production-legacy', role='agent',
            agent_id='agent_demo_001', sha256=hash_token(world.legacy))])
        app = create_app(settings=settings, service=world.deps.risk_service, platform=world.deps,
                         registry=registry)
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 49035)) as client:
            for path in ['/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect']:
                assert client.get(path).status_code == 401
                result = client.get(path, headers={'Authorization': 'Bearer ' + world.owner})
                # v1 owner credentials never authenticate legacy paths.
                assert result.status_code == 401
                result = client.get(path, headers={'Authorization': 'Bearer ' + world.legacy})
                assert result.status_code == 404


def test_malformed_totp_lockout_is_durable_and_ordinary_bearer_never_gains_mfa(monkeypatch):
    monkeypatch.setenv('AGENTPAL_MFA_ENCRYPTION_KEY', base64.urlsafe_b64encode(os.urandom(32)).decode())
    with edge_world() as world:
        identity = world.deps.owner_authenticator
        now = [datetime.now(timezone.utc)]
        identity.clock = lambda: now[0]
        enrollment = identity.begin_totp_enrollment(world.owner)
        for bad in [None, '１２３４５６', 'NaN', '1234567', '']:
            with pytest.raises(IdentityError):
                identity.confirm_totp_enrollment(world.owner, bad)
        assert identity.authenticate(world.owner).mfa_satisfied is False
        reopened = OwnerIdentityService(IdentityStore(identity.store.path), clock=lambda: now[0])
        try:
            code = totp(base64.b32decode(enrollment.secret), int(now[0].timestamp()))
            with pytest.raises(IdentityError):
                reopened.confirm_totp_enrollment(world.owner, code)
            with pytest.raises(IdentityError):
                reopened.begin_totp_enrollment(world.owner)
            now[0] += timedelta(minutes=5)
            code = totp(base64.b32decode(enrollment.secret), int(now[0].timestamp()))
            reopened.confirm_totp_enrollment(world.owner, code)
            assert reopened.authenticate(world.owner).mfa_satisfied is False
        finally:
            reopened.store.close()
