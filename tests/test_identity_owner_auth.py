"""S1: durable owner authentication (OwnerAuthenticator, SEC-15/SEC-23)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agentpal.identity import IdentityError, IdentityStore, OwnerIdentityService
from agentpal.identity.__main__ import main as cli_main
from agentpal.infra_contracts import AuthenticatedOwner, OwnerAuthenticator, OwnerAuthError

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make(db=":memory:", clock=None, **kw):
    clock = clock or Clock()
    svc = OwnerIdentityService(IdentityStore(db), clock=clock, **kw)
    svc.create_org("org_acme", "Acme")
    svc.create_owner("owner_alice", "Alice", org_id="org_acme")
    svc.create_owner("owner_bob", "Bob")
    return svc, clock


def test_service_implements_the_contract():
    svc, _ = make()
    assert isinstance(svc, OwnerAuthenticator)


def test_token_round_trip_yields_an_authenticated_owner():
    svc, _ = make()
    token, info = svc.issue_token("owner_alice", timedelta(days=1), label="laptop")
    assert token.startswith("apo_" + info.credential_id + "_")
    owner = svc.authenticate(token)
    assert isinstance(owner, AuthenticatedOwner)
    assert owner.owner_id == "owner_alice" and owner.org_id == "org_acme"
    assert owner.session_id == info.credential_id and owner.auth_method == "owner_api_token"
    assert owner.mfa_satisfied is False and owner.authenticated_at == T0
    assert svc.authenticate("Bearer " + token).owner_id == "owner_alice"
    assert svc.authenticate(svc.issue_token("owner_bob")[0]).org_id is None


def test_only_a_hash_is_stored(tmp_path):
    db = tmp_path / "identity.db"
    svc, _ = make(db)
    token, info = svc.issue_token("owner_alice")
    secret = token.split("_", 2)[2]
    svc.store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert secret.encode() not in raw and token.encode() not in raw
    row = IdentityStore(db).query("SELECT * FROM identity_owner_credentials")[0]
    assert secret not in str(row) and len(row["secret_sha256"]) == 64


@pytest.mark.parametrize("bad", ["", "Bearer ", "apo_", "apo_xyz_abc", "garbage",
                                 "apk_0123456789abcdef01234567_" + "A" * 43,
                                 "apo_0123456789abcdef01234567_" + "A" * 43])
def test_malformed_unknown_and_agent_keys_are_refused(bad):
    svc, _ = make()
    svc.issue_token("owner_alice")
    with pytest.raises(OwnerAuthError) as e:
        svc.authenticate(bad)
    assert str(e.value) == "invalid_owner_credential"


def test_agent_key_with_a_valid_owner_secret_is_still_refused():
    """Credentials never cross over: the same id+secret under the apk_ prefix fails."""
    svc, _ = make()
    token, _ = svc.issue_token("owner_alice")
    with pytest.raises(OwnerAuthError):
        svc.authenticate("apk_" + token[4:])


def test_wrong_secret_is_refused_and_not_echoed():
    svc, _ = make()
    token, _ = svc.issue_token("owner_alice")
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(OwnerAuthError) as e:
        svc.authenticate(forged)
    assert forged not in str(e.value) and token not in str(e.value)


def test_expiry_boundary():
    svc, clock = make()
    token, _ = svc.issue_token("owner_alice", timedelta(hours=1))
    clock.t = T0 + timedelta(minutes=59, seconds=59)
    svc.authenticate(token)
    clock.t = T0 + timedelta(hours=1)
    with pytest.raises(OwnerAuthError):
        svc.authenticate(token)


def test_revoked_token_is_refused():
    svc, _ = make()
    token, info = svc.issue_token("owner_alice")
    other, _ = svc.issue_token("owner_alice")
    svc.revoke_token(info.credential_id)
    with pytest.raises(OwnerAuthError):
        svc.authenticate(token)
    assert svc.authenticate(other).owner_id == "owner_alice"


def test_disabled_owner_is_refused_and_cannot_get_new_tokens():
    svc, _ = make()
    token, _ = svc.issue_token("owner_alice")
    svc.disable_owner("owner_alice")
    with pytest.raises(OwnerAuthError):
        svc.authenticate(token)
    with pytest.raises(IdentityError, match="OWNER_DISABLED"):
        svc.issue_token("owner_alice")


def test_ttl_is_bounded():
    svc, _ = make(max_token_ttl=timedelta(days=7))
    with pytest.raises(IdentityError, match="INVALID_TTL"):
        svc.issue_token("owner_alice", timedelta(days=8))
    with pytest.raises(IdentityError, match="INVALID_TTL"):
        svc.issue_token("owner_alice", timedelta(0))


def test_admin_refusals():
    svc, _ = make(is_agent_id=lambda i: i == "alice_payment")
    with pytest.raises(IdentityError, match="OWNER_EXISTS"):
        svc.create_owner("owner_alice")
    with pytest.raises(IdentityError, match="UNKNOWN_ORG"):
        svc.create_owner("owner_carol", org_id="org_none")
    with pytest.raises(IdentityError, match="INVALID_OWNER_ID"):
        svc.create_owner("x")
    with pytest.raises(IdentityError, match="OWNER_ID_IS_AN_AGENT"):
        svc.create_owner("alice_payment")
    with pytest.raises(IdentityError, match="UNKNOWN_OWNER"):
        svc.issue_token("owner_nobody")


def test_tokens_survive_a_restart(tmp_path):
    db = tmp_path / "identity.db"
    svc, clock = make(db)
    token, _ = svc.issue_token("owner_alice")
    svc.store.close()
    svc2 = OwnerIdentityService(IdentityStore(db), clock=clock)
    assert svc2.authenticate(token).owner_id == "owner_alice"


# ----------------------------------------------------- forged AuthenticatedOwner objects
def _forge(**kw):
    base = dict(owner_id="owner_alice", org_id="org_acme", session_id="0" * 24,
                auth_method="owner_api_token", authenticated_at=T0)
    base.update(kw)
    return AuthenticatedOwner(**base)


def test_session_revalidation_refuses_forged_objects():
    svc, clock = make()
    token, info = svc.issue_token("owner_alice", timedelta(hours=1))
    bob_token, bob_info = svc.issue_token("owner_bob")
    real = svc.authenticate(token)
    svc.assert_session_active(real)
    for forged in (_forge(),                                          # unknown session
                   _forge(session_id=bob_info.credential_id),         # someone else's
                   _forge(session_id=info.credential_id, owner_id="owner_bob"),
                   _forge(session_id=info.credential_id, org_id=None),
                   _forge(session_id=info.credential_id, mfa_satisfied=True),
                   _forge(session_id=info.credential_id, auth_method="owner_session"),
                   _forge(session_id="not-a-credential-id")):
        with pytest.raises(OwnerAuthError):
            svc.assert_session_active(forged)
    svc.revoke_token(info.credential_id)
    with pytest.raises(OwnerAuthError):
        svc.assert_session_active(real)


def test_session_revalidation_refuses_expired_sessions():
    svc, clock = make()
    token, _ = svc.issue_token("owner_alice", timedelta(hours=1))
    real = svc.authenticate(token)
    clock.t = T0 + timedelta(hours=2)
    with pytest.raises(OwnerAuthError):
        svc.assert_session_active(real)


# ------------------------------------------------------------------- CLI
def test_cli_bootstrap_issue_list_revoke(tmp_path, capsys):
    db = str(tmp_path / "id.db")
    assert cli_main(["--db", db, "create-org", "--org-id", "org_acme", "--name", "Acme"]) == 0
    assert cli_main(["--db", db, "create-owner", "--owner-id", "owner_alice",
                     "--org-id", "org_acme"]) == 0
    capsys.readouterr()
    assert cli_main(["--db", db, "issue-token", "--owner-id", "owner_alice",
                     "--ttl-days", "2"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert token.startswith("apo_") and "shown once" in out.err
    svc = OwnerIdentityService(IdentityStore(db))
    owner = svc.authenticate(token)
    assert owner.owner_id == "owner_alice"
    assert cli_main(["--db", db, "list-tokens", "--owner-id", "owner_alice"]) == 0
    listed = capsys.readouterr().out
    assert owner.session_id in listed and token.split("_", 2)[2] not in listed
    assert cli_main(["--db", db, "revoke-token", "--credential-id", owner.session_id]) == 0
    with pytest.raises(OwnerAuthError):
        svc.authenticate(token)
    assert cli_main(["--db", db, "issue-token", "--owner-id", "nobody_here"]) == 2


def test_cli_runs_as_a_module(tmp_path):
    db = str(tmp_path / "id.db")
    r = subprocess.run([sys.executable, "-m", "agentpal.identity", "--db", db,
                        "create-owner", "--owner-id", "owner_zed"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "owner_zed" in r.stdout
