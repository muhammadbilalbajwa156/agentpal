"""Bounded loopback smoke, own CLI process on assigned port and private DBs only."""
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx
import pytest

PROJECT = Path(__file__).resolve().parents[3]


def test_own_cli_bootstrap_and_unsigned_webhook_never_confirm():
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', 8203))
        except OSError:
            pytest.skip('H3 assigned port 8203 occupied; refuse traffic to another server')
    root = Path('/tmp/m8-h3')
    root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='cli-', dir=root) as directory:
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ('AGENTPAL_', 'PAYPAL_', 'CHANNEL3_', 'KERNEL_'))}
        env.update(AGENTPAL_DB_PATH=str(Path(directory) / 'risk.db'),
            AGENTPAL_IDENTITY_DB_PATH=str(Path(directory) / 'identity.db'),
            AGENTPAL_WALLET_DB_PATH=str(Path(directory) / 'wallet.db'),
            AGENTPAL_RATE_LIMIT_DB_PATH=str(Path(directory) / 'rate.db'),
            AGENTPAL_ANALYZER_CHAIN='heuristic', AGENTPAL_AUTH_MODE='required',
            AGENTPAL_ALLOWED_HOSTS='127.0.0.1,localhost')
        bootstrap = subprocess.run([sys.executable, '-m', 'agentpal', 'new-owner', 'h3_cli_owner'],
            cwd=PROJECT, env=env, capture_output=True, text=True, timeout=20)
        assert bootstrap.returncode == 0, 'CLI bootstrap failed; raw output withheld'
        match = re.search(r'apo_[A-Za-z0-9_-]+', bootstrap.stdout + bootstrap.stderr)
        assert match, 'CLI did not issue an owner credential'
        owner = {'Authorization': 'Bearer ' + match.group()}
        process = subprocess.Popen([sys.executable, '-m', 'agentpal', 'serve', '--platform',
            '--host', '127.0.0.1', '--port', '8203'], cwd=PROJECT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url='http://127.0.0.1:8203', trust_env=False, timeout=2) as client:
                for _ in range(100):
                    assert process.poll() is None, 'Own H3 CLI exited before readiness'
                    try:
                        if client.get('/v1/health').status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    pytest.fail('Own H3 CLI readiness timeout')
                assert process.poll() is None
                response = client.post('/v1/agents', headers=owner,
                    json={'name': 'H3 CLI agent', 'capabilities': ['wallet.read']})
                assert response.status_code == 201, 'Own CLI refused its bootstrap owner'
                agent = {'Authorization': 'Bearer ' + response.json()['api_key']}
                assert client.get('/v1/me', headers=agent).status_code == 200
                assert client.post('/v1/providers/paypal/webhooks', json={
                    'verified': True, 'status': 'COMPLETED'}).status_code == 400
                assert client.get('/v1/health').status_code == 200
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
