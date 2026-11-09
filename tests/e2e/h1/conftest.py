"""H1-only, offline real-service and loopback CLI harnesses.

No provider environment is inherited by children. Issued credentials stay in memory;
only legacy credential hashes, databases and scrubbed server logs reach /tmp/m8-h1.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from agentpal.api import create_app
from agentpal.auth import TokenEntry, TokenRegistry, generate_token, hash_token
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform

ROOT = Path('/tmp/m8-h1')
PROJECT = Path(__file__).resolve().parents[3]
PORT = 8201


@dataclass(repr=False)
class EdgeWorld:
    client: object
    deps: object
    settings: Settings
    owner: str = field(repr=False)
    agent: str = field(repr=False)
    legacy: str = field(repr=False)


@contextmanager
def edge_world(**overrides):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='asgi-', dir=ROOT) as directory:
        values = dict(db_path=directory + '/risk.db', environment='development',
                      auth_mode='required', api_tokens_file=None, analyzer_chain=['heuristic'],
                      allowed_hosts=['127.0.0.1', 'localhost', '::1'],
                      rate_limit_backend='sqlite', rate_limit_db_path=directory + '/rate.db',
                      rate_limit_per_peer_per_minute=5000,
                      rate_limit_per_token_per_minute=5000, require_mfa_for_approval=False,
                      webhook_rate_limit_global_per_minute=120,
                      webhook_rate_limit_per_peer_per_minute=30,
                      public_webhook_enabled=False, tls_certfile=None, tls_keyfile=None)
        values.update(overrides)
        settings = Settings(**values)
        deps = build_platform(settings=settings, environ={})
        deps.owner_authenticator.create_owner('h1_owner')
        owner, _ = deps.owner_authenticator.issue_token('h1_owner')
        legacy = generate_token()
        registry = TokenRegistry(tokens=[TokenEntry(id='h1-legacy', role='agent',
            agent_id='agent_demo_001', sha256=hash_token(legacy))])
        app = create_app(settings=settings, service=deps.risk_service, platform=deps,
                         registry=registry)
        try:
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 49001),
                            raise_server_exceptions=False, follow_redirects=False) as client:
                # Provision through the actual owner-authenticated route.
                response = client.post('/v1/agents', headers={'Authorization': 'Bearer ' + owner},
                                       json={'name': 'H1 edge agent'})
                assert response.status_code == 201
                yield EdgeWorld(client, deps, settings, owner, response.json()['api_key'], legacy)
        finally:
            deps.close()


@pytest.fixture(scope='module')
def edge():
    with edge_world() as world:
        yield world


@dataclass(repr=False)
class LocalServer:
    client: httpx.Client
    process: subprocess.Popen
    directory: Path
    owner: str = field(repr=False)
    legacy: str = field(repr=False)
    logs: Path


@contextmanager
def local_server(tls=None):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tcp-', dir=ROOT) as directory:
        directory = Path(directory)
        # Check availability without sending any traffic to somebody else's listener.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', PORT))
        legacy = generate_token()
        tokenfile = directory / 'legacy.json'
        tokenfile.write_text(TokenRegistry(tokens=[TokenEntry(id='h1-legacy', role='agent',
            agent_id='agent_demo_001', sha256=hash_token(legacy))]).model_dump_json())
        child_env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'PYTHONPATH')
                     if k in os.environ}
        child_env.update(AGENTPAL_DB_PATH=str(directory / 'risk.db'),
            AGENTPAL_IDENTITY_DB_PATH=str(directory / 'identity.db'),
            AGENTPAL_WALLET_DB_PATH=str(directory / 'wallet.db'),
            AGENTPAL_RATE_LIMIT_DB_PATH=str(directory / 'rate.db'),
            AGENTPAL_AUTH_MODE='required', AGENTPAL_ENV='development',
            AGENTPAL_ANALYZER_CHAIN='heuristic', AGENTPAL_ALLOWED_HOSTS='127.0.0.1,localhost,::1',
            AGENTPAL_API_TOKENS_FILE=str(tokenfile), AGENTPAL_RATE_LIMIT_PER_PEER='5000',
            AGENTPAL_RATE_LIMIT_PER_TOKEN='5000')
        bootstrap = subprocess.run([sys.executable, '-m', 'agentpal', 'new-owner', 'h1_owner'],
            cwd=PROJECT, env=child_env, capture_output=True, timeout=15)
        assert bootstrap.returncode == 0, 'local owner bootstrap failed (output withheld)'
        owner = bootstrap.stdout.decode().splitlines()[0]
        command = [sys.executable, '-m', 'agentpal', 'serve', '--platform',
                   '--host', '127.0.0.1', '--port', str(PORT)]
        scheme, verify = 'http', True
        if tls is not None:
            import ssl
            cert, key = tls
            command.extend(['--ssl-certfile', cert, '--ssl-keyfile', key])
            scheme, verify = 'https', ssl.create_default_context(cafile=cert)
        logs = directory / 'server.log'
        with logs.open('wb') as output:
            process = subprocess.Popen(command, cwd=PROJECT, env=child_env,
                                       stdout=output, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'{scheme}://127.0.0.1:{PORT}', trust_env=False,
                                  verify=verify, timeout=2, follow_redirects=False) as client:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        assert process.poll() is None, 'own CLI server exited before readiness'
                        try:
                            if client.get('/health').status_code == 200:
                                break
                        except httpx.TransportError:
                            time.sleep(0.05)
                    else:
                        pytest.fail('own CLI server readiness deadline exceeded')
                    yield LocalServer(client, process, directory, owner, legacy, logs)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture(scope='module')
def tcp():
    with local_server() as server:
        yield server


def raw_request(target=b'/v1/agents', headers=(), method=b'GET', body=b''):
    with socket.create_connection(('127.0.0.1', PORT), timeout=2) as connection:
        connection.sendall(method + b' ' + target + b' HTTP/1.1\r\n' +
            b'Host: 127.0.0.1:8201\r\nConnection: close\r\n' +
            b''.join(k + b': ' + v + b'\r\n' for k, v in headers) + b'\r\n' + body)
        data = b''
        while len(data) < 65536:
            chunk = connection.recv(65536 - len(data))
            if not chunk:
                break
            data += chunk
    status = int(data.split(b' ', 2)[1])
    return status, data


def legacy_body(**overrides):
    body = dict(request_id='h1_edge_request', agent_id='agent_demo_001', amount='1.00',
                currency='USD', merchant_name='H1 merchant', merchant_domain='example.com',
                purpose='H1 input validation', evidence=[])
    body.update(overrides)
    return json.dumps(body).encode()
