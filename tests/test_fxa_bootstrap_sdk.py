"""FX-A hosted identity and SDK regressions; disposable credentials only."""
import asyncio
from datetime import datetime
import secrets
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agentpal.__main__ import _bootstrap_hosted_owner
from agentpal.identity import OwnerIdentityService
from agentpal.identity.store import IdentityStore
from test_platform_fixtures import settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk' / 'python'))
from agentpal_sdk import AgentClient, OwnerClient  # noqa: E402


@pytest.mark.parametrize('days', [None, '1', '45', '90'])
@pytest.mark.parametrize('primary', [True, False])
def test_hosted_bootstrap_both_owners_idempotent(tmp_path, monkeypatch, capsys, caplog, days, primary):
    path = tmp_path / 'identity.db'
    monkeypatch.setenv('AGENTPAL_IDENTITY_DB_PATH', str(path))
    for name in ('AGENTPAL_BOOTSTRAP_OWNER_ID', 'AGENTPAL_BOOTSTRAP_OWNER_TOKEN',
                 'AGENTPAL_OWNER_TOKEN_DAYS'):
        monkeypatch.delenv(name, raising=False)
    tokens = {}
    for prefix, owner in ([('BOOTSTRAP', 'hosted')] if primary else []) + [('JUDGE', 'judge')]:
        token = 'apo_' + secrets.token_hex(12) + '_' + secrets.token_urlsafe(32)
        tokens[owner] = token
        monkeypatch.setenv(f'AGENTPAL_{prefix}_OWNER_ID', owner)
        monkeypatch.setenv(f'AGENTPAL_{prefix}_OWNER_TOKEN', token)
    if days is not None:
        monkeypatch.setenv('AGENTPAL_OWNER_TOKEN_DAYS', days)
    _bootstrap_hosted_owner(settings(db_path=str(tmp_path / 'risk.db')))
    identity = OwnerIdentityService(IdentityStore(str(path)))
    rows = identity.store.query('SELECT * FROM identity_owner_credentials')
    assert len(rows) == len(tokens)
    for row in rows:
        assert (datetime.fromisoformat(row['expires_at']) -
                datetime.fromisoformat(row['created_at'])).days == int(days or 30)
    assert identity.get_owner('judge').display_name == 'Judge workspace'
    for token in tokens.values():
        assert identity.authenticate(token).owner_id in tokens
    identity.revoke_token(rows[0]['credential_id'])
    identity.store.close()
    _bootstrap_hosted_owner(settings(db_path=str(tmp_path / 'risk.db')))
    identity = OwnerIdentityService(IdentityStore(str(path)))
    after = identity.store.query('SELECT * FROM identity_owner_credentials')
    assert len(after) == len(rows)
    assert [r['expires_at'] for r in after] == [r['expires_at'] for r in rows]
    assert after[0]['revoked_at'] is not None
    identity.store.close()
    output = str(capsys.readouterr()) + caplog.text + str(after)
    for token in tokens.values():
        assert token not in output


@pytest.mark.parametrize('value', ['', '0', '-1', '91', '1.5', 'abc'])
def test_invalid_bootstrap_lifetime_refuses_even_without_owners(tmp_path, monkeypatch, value):
    monkeypatch.setenv('AGENTPAL_OWNER_TOKEN_DAYS', value)
    monkeypatch.delenv('AGENTPAL_BOOTSTRAP_OWNER_ID', raising=False)
    monkeypatch.delenv('AGENTPAL_BOOTSTRAP_OWNER_TOKEN', raising=False)
    with pytest.raises(SystemExit, match='AGENTPAL_OWNER_TOKEN_DAYS'):
        _bootstrap_hosted_owner(settings(db_path=str(tmp_path / 'risk.db')))


def test_new_owner_help_has_real_bound():
    result = subprocess.run([sys.executable, '-m', 'agentpal', 'new-owner', '--help'],
                            capture_output=True, text=True, check=True)
    assert 'max 90' in result.stdout


@pytest.mark.parametrize('suffix', ['', '/', '/v1', '/v1/'])
@pytest.mark.parametrize('client_type,prefix', [(OwnerClient, 'apo_'), (AgentClient, 'apk_')])
def test_sdk_base_url_has_exactly_one_version(suffix, client_type, prefix):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})
    async def run():
        async with client_type(base_url='https://example.test' + suffix,
                               credential=prefix + secrets.token_urlsafe(32),
                               transport=httpx.MockTransport(handler)) as client:
            await client.health()
            await client.get_payment_request('pay_example')
    asyncio.run(run())
    assert seen == ['/v1/health', '/v1/payment-requests/pay_example']
