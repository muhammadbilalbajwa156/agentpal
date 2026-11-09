"""Agent API keys: format, one-time display, hashed storage, rotation, revocation,
suspension, and that the plaintext key never leaks (DB, logs, audit, error bodies)."""

from __future__ import annotations

import sqlite3

import pytest

from agentpal.infra_contracts import AgentAuthError, AgentCredentialVerifier
from agentpal.platform.credentials import KEY_RE, hash_key
from agentpal.wallet.store import WALLET_SCHEMA_VERSION, WalletStore

from test_platform_fixtures import ALL_PAY, OWNER_A, build_world


@pytest.fixture
def w():
    return build_world()


def test_key_format_and_verifier_contract(w):
    agent_id, key = w.register()
    m = KEY_RE.fullmatch(key)
    assert m and len(m.group(2)) == 43            # 256-bit secret
    verifier = w.platform.verifier
    assert isinstance(verifier, AgentCredentialVerifier)
    ctx = verifier.verify(key)
    assert (ctx.agent_id, ctx.owner_id, ctx.role) == (agent_id, "owner_alice", "external")


def test_only_the_digest_is_stored(w):
    agent_id, key = w.register()
    rows = w.wallet_store.query("SELECT * FROM platform_agent_keys WHERE agent_id=?",
                                (agent_id,))
    assert len(rows) == 1 and rows[0]["key_sha256"] == hash_key(key)


def _dump(store) -> str:
    conn = store._conn
    return "\n".join(conn.iterdump())


def test_plaintext_key_never_leaks(capsys):
    w = build_world()
    agent_id, key = w.register()
    w.grant(agent_id, ALL_PAY)
    w.wallet(agent_id)
    secret = key.split("_", 2)[2]
    # use the key, fail with it, rotate, revoke
    assert w.pay(key).status_code == 201
    bad = key[:-1] + ("A" if key[-1] != "A" else "B")
    responses = [w.client.get("/v1/me", headers=w.h(bad)),
                 w.client.get("/v1/agents", headers=w.h(key)),
                 w.client.post("/assess", json={}, headers=w.h(key)),
                 w.client.post("/v1/wallets", json={"agent_id": key}, headers=w.h(OWNER_A))]
    rot = w.client.post(f"/v1/agents/{agent_id}/credentials", json={},
                        headers=w.h(OWNER_A))
    assert rot.status_code == 201
    for r in responses + [w.client.get(f"/v1/agents/{agent_id}", headers=w.h(OWNER_A)),
                          w.client.get("/v1/me", headers=w.h(key))]:
        assert secret not in r.text and key not in r.text
    dumps = _dump(w.wallet_store) + _dump(w.risk.store)
    assert key not in dumps and secret not in dumps
    new_key = rot.json()["api_key"]
    assert new_key not in dumps
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    assert new_key.split("_", 2)[2] not in out.out + out.err


@pytest.mark.parametrize("token", ["apk_", "apk_0123456789abcdef_short",
                                   "apk_0123456789abcdef_" + "A" * 43,
                                   "apk_ZZZZ456789abcdef_" + "A" * 43, "apk_" + "x" * 80])
def test_malformed_or_unknown_keys_are_401(w, token):
    r = w.client.get("/v1/me", headers=w.h(token))
    assert r.status_code == 401 and r.json() == {"error": "invalid_token"}
    with pytest.raises(AgentAuthError):
        w.platform.verifier.verify(token)


def test_wrong_secret_for_known_key_id_is_refused(w):
    _, key = w.register()
    forged = key[:21] + ("A" * 43 if not key.endswith("A" * 43) else "B" * 43)
    assert w.client.get("/v1/me", headers=w.h(forged)).status_code == 401


def test_rotation_without_overlap_kills_the_old_key(w):
    agent_id, old = w.register()
    r = w.client.post(f"/v1/agents/{agent_id}/credentials", json={"overlap_seconds": 0},
                      headers=w.h(OWNER_A))
    assert r.status_code == 201 and r.json()["previous_key_expires_at"] is None
    new = r.json()["api_key"]
    assert w.client.get("/v1/me", headers=w.h(old)).status_code == 401
    assert w.client.get("/v1/me", headers=w.h(new)).status_code == 200


def test_rotation_overlap_window(w):
    agent_id, old = w.register()
    r = w.client.post(f"/v1/agents/{agent_id}/credentials", json={"overlap_seconds": 600},
                      headers=w.h(OWNER_A))
    new = r.json()["api_key"]
    assert r.json()["previous_key_expires_at"] is not None
    assert w.client.get("/v1/me", headers=w.h(old)).status_code == 200
    w.clock.advance(seconds=599)
    assert w.client.get("/v1/me", headers=w.h(old)).status_code == 200
    w.clock.advance(seconds=2)
    assert w.client.get("/v1/me", headers=w.h(old)).status_code == 401
    assert w.client.get("/v1/me", headers=w.h(new)).status_code == 200


def test_at_most_two_live_keys(w):
    agent_id, k1 = w.register()
    keys = [k1]
    for _ in range(3):
        r = w.client.post(f"/v1/agents/{agent_id}/credentials",
                          json={"overlap_seconds": 3600}, headers=w.h(OWNER_A))
        keys.append(r.json()["api_key"])
    live = [k for k in keys if w.client.get("/v1/me", headers=w.h(k)).status_code == 200]
    assert live == keys[-2:]
    statuses = [k["status"] for k in w.client.get(
        f"/v1/agents/{agent_id}", headers=w.h(OWNER_A)).json()["keys"]]
    assert statuses.count("live") == 2


def test_overlap_above_ceiling_is_422(w):
    agent_id, _ = w.register()
    r = w.client.post(f"/v1/agents/{agent_id}/credentials",
                      json={"overlap_seconds": 86_401}, headers=w.h(OWNER_A))
    assert r.status_code == 422


def test_revocation(w):
    agent_id, key = w.register()
    key_id = KEY_RE.fullmatch(key).group(1)
    r = w.client.delete(f"/v1/agents/{agent_id}/credentials/{key_id}", headers=w.h(OWNER_A))
    assert r.status_code == 204
    assert w.client.get("/v1/me", headers=w.h(key)).status_code == 401
    r = w.client.delete(f"/v1/agents/{agent_id}/credentials/0123456789abcdef",
                        headers=w.h(OWNER_A))
    assert r.status_code == 404
    actions = [e["action"] for e in w.wallet_store.query(
        "SELECT action FROM platform_credential_events WHERE agent_id=? ORDER BY id",
        (agent_id,))]
    assert actions == ["issue", "revoke"]


def test_suspended_agent_key_is_refused_until_reactivated(w):
    agent_id, key = w.register()
    assert w.client.post(f"/v1/agents/{agent_id}/suspend",
                         headers=w.h(OWNER_A)).json()["status"] == "suspended"
    assert w.client.get("/v1/me", headers=w.h(key)).status_code == 401
    w.client.post(f"/v1/agents/{agent_id}/reactivate", headers=w.h(OWNER_A))
    assert w.client.get("/v1/me", headers=w.h(key)).status_code == 200


def test_wallet_schema_v1_upgrades_in_place(tmp_path):
    path = tmp_path / "wallet.db"
    WalletStore(path).close()
    conn = sqlite3.connect(path)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                                "name LIKE 'platform_%'").fetchall():
        conn.execute(f"DROP TABLE {name}")
    conn.execute("UPDATE wallet_meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    ws = WalletStore(path)
    names = {r["name"] for r in ws.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"platform_agent_keys", "platform_wallets", "platform_idempotency"} <= names
    assert ws.query("SELECT value FROM wallet_meta WHERE key='schema_version'")[0][
        "value"] == str(WALLET_SCHEMA_VERSION) == "2"
