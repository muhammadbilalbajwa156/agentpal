"""Pasted credentials must not enter validation/audit or business storage."""

import pytest

from test_platform_fixtures import OWNER_A, build_world


def _assert_absent(w, key, response, output):
    secret = key.split("_", 2)[2]
    dumps = "\n".join(w.wallet_store._conn.iterdump()) + "\n".join(w.risk.store._conn.iterdump())
    for text in (response.text, dumps, output.out + output.err):
        assert key not in text
        assert secret not in text


def test_credential_as_extra_field_name_does_not_leak(monkeypatch, capsys):
    monkeypatch.setattr("agentpal.platform.credentials.secrets.token_urlsafe", lambda n: "x" * 43)
    w = build_world()
    _, key = w.register()
    r = w.client.post("/v1/agents", json={"name": "bot", key: "oops"}, headers=w.h(OWNER_A))
    assert r.status_code == 422
    _assert_absent(w, key, r, capsys.readouterr())


@pytest.mark.parametrize("where", ["idempotency", "name", "metadata", "purpose"])
def test_credentials_in_business_inputs_are_refused(where, capsys):
    w = build_world()
    _, key, _ = w.funded_agent()
    if where == "idempotency":
        r = w.pay(key, idem=key)
    elif where == "purpose":
        r = w.pay(key, purpose=f"accidentally pasted {key}")
    else:
        body = {"name": key} if where == "name" else {"name": "bot", "metadata": {"token": key}}
        r = w.client.post("/v1/agents", json=body, headers=w.h(OWNER_A))
    assert r.status_code == 422
    _assert_absent(w, key, r, capsys.readouterr())
