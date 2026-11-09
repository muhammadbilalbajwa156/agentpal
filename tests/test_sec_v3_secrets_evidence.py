"""No credentials in owned metadata, logs or errors; evidence remains untrusted."""
import json
import logging
import secrets

import pytest

from agentpal.api import _JsonFormatter
from agentpal.identity import IdentityStore, OwnerIdentityService
from agentpal.wallet.models import PaymentDraft


def credential(prefix="apo"):
    return prefix + "_" + secrets.token_hex(12) + "_" + secrets.token_urlsafe(32)


def test_json_logging_redacts_messages_keys_nested_extras_and_environment(monkeypatch):
    token, provider_secret = credential(), secrets.token_urlsafe(32)
    monkeypatch.setenv("PAYPAL_SANDBOX_CLIENT_SECRET", provider_secret)
    record = logging.makeLogRecord({"name": "agentpal.test", "levelname": "ERROR",
        "msg": "Bearer %s provider=%s", "args": (token, provider_secret),
        "payload": {token: {"authorization": token, "totp_code": "123456", "nested": [token]}},
        "seed": secrets.token_urlsafe(20)})
    rendered = _JsonFormatter().format(record)
    assert token not in rendered and provider_secret not in rendered and "123456" not in rendered
    assert json.loads(rendered)["msg"]


@pytest.mark.parametrize("where", ["label", "owner", "organization", "draft"])
def test_credentials_in_metadata_are_refused(where):
    token = credential()
    svc = OwnerIdentityService(IdentityStore())
    svc.create_owner("owner_safe")
    with pytest.raises(ValueError):
        if where == "label":
            svc.issue_token("owner_safe", label=token)
        elif where == "owner":
            svc.create_owner("owner_other", display_name=token)
        elif where == "organization":
            svc.create_org("org_other", token)
        else:
            PaymentDraft(amount="1.00", currency="USD", merchant_name="merchant", purpose=token)
    assert token not in "\n".join(svc.store._conn.iterdump())


def test_cli_error_and_old_label_output_never_echo_credentials(tmp_path, capsys):
    from agentpal.identity.__main__ import main
    token = credential()
    path = tmp_path / "identity.db"
    svc = OwnerIdentityService(IdentityStore(path))
    svc.create_owner("owner_safe")
    _, info = svc.issue_token("owner_safe")
    with svc.store.tx() as c:  # simulate older, unsafe metadata
        c.execute("UPDATE identity_owner_credentials SET label=? WHERE credential_id=?",
                  (token, info.credential_id))
    assert main(["--db", str(path), "list-tokens", "--owner-id", "owner_safe"]) == 0
    assert token not in capsys.readouterr().out
    assert main(["--db", str(path), "create-owner", "--owner-id", token]) == 2
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_access_log_never_includes_query_or_pasted_credential():
    from agentpal.api import _SafeAccessFormatter
    token = credential()
    record = logging.makeLogRecord({"name": "uvicorn.access", "levelname": "INFO", "msg": "%s %s %s %s %s",
        "args": ("127.0.0.1:1", "GET", "/v1/agents/" + token + "?secret=unknownvalue", "1.1", 404)})
    rendered = _SafeAccessFormatter().format(record)
    assert token not in rendered and "unknownvalue" not in rendered
    assert "404" in rendered


def test_evidence_corruption_is_rejected_without_becoming_verified(tmp_path):
    from agentpal.tools import EvidenceError, EvidenceStore
    store = EvidenceStore(tmp_path)
    digest = store.put(b"observed bytes")
    path = tmp_path / digest[:2] / digest
    path.chmod(0o600)
    path.write_bytes(b"altered")
    with pytest.raises(EvidenceError):
        store.get(digest)
    assert not store.verify(digest)


@pytest.mark.parametrize("prefix", ["apk", "apo", "apt"])
def test_legacy_assess_refuses_pasted_credential_before_persistence(prefix):
    from test_sec_auth import auth, body, service, TOKEN_A
    from agentpal.auth import TokenRegistry, hash_token
    from agentpal.config import Settings
    from agentpal.api import create_app
    from fastapi.testclient import TestClient
    token = credential(prefix)
    risk = service()
    registry = TokenRegistry.model_validate({"tokens": [{"id": "safe", "role": "agent",
        "agent_id": "agent_demo_001", "sha256": hash_token(TOKEN_A)}]})
    app = create_app(service=risk, settings=Settings(auth_mode="required"), registry=registry)
    with TestClient(app) as client:
        response = client.post("/assess", json={**body(), "purpose": token}, headers=auth(TOKEN_A))
    assert response.status_code == 422
    assert token not in response.text
    assert token not in "\n".join(risk.store._conn.iterdump())


def test_access_logs_redact_percent_encoded_credentials():
    from agentpal.api import _SafeAccessFormatter
    token = credential()
    encoded = "".join(f"%{byte:02X}" for byte in token.encode())
    record = logging.makeLogRecord({"name": "uvicorn.access", "levelname": "INFO", "msg": "access",
        "args": ("127.0.0.1:1", "GET", "/v1/agents/" + encoded, "1.1", 404)})
    rendered = _SafeAccessFormatter().format(record)
    assert encoded not in rendered and token not in rendered


@pytest.mark.parametrize("prefix", ["sk_test_", "sk-proj-", "EAA"])
def test_common_secret_patterns_are_redacted_and_refused(prefix):
    import string
    value = prefix + "".join(secrets.choice(string.ascii_letters) for _ in range(40))
    record = logging.makeLogRecord({"name": "agentpal.test", "levelname": "ERROR",
                                    "msg": "value=%s", "args": (value,)})
    rendered = _JsonFormatter().format(record)
    assert value not in rendered and prefix not in rendered
    with pytest.raises(ValueError):
        PaymentDraft(amount="1.00", currency="USD", merchant_name="merchant", purpose=value)
