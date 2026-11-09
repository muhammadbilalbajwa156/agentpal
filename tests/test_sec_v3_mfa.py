"""Real TOTP, durable proof, replay/lockout and HTTP approval, all offline."""
import base64
from datetime import timedelta
import multiprocessing as mp
import os

import pytest

from agentpal.identity import IdentityError, IdentityStore, OwnerIdentityService
from agentpal.infra_contracts import OwnerAuthError
from test_identity_owner_auth import Clock


@pytest.fixture
def encryption_key(monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("AGENTPAL_MFA_ENCRYPTION_KEY", key)
    return key


def identity(path=":memory:", clock=None):
    clock = clock or Clock()
    svc = OwnerIdentityService(IdentityStore(path), clock=clock)
    svc.create_owner("owner_alice")
    return svc, clock, svc.issue_token("owner_alice")[0]


def code(secret, clock):
    from agentpal.identity.totp import totp
    return totp(base64.b32decode(secret), int(clock().timestamp()))


def enrolled(svc, token, clock):
    pending = svc.begin_totp_enrollment(token)
    svc.confirm_totp_enrollment(token, code(pending.secret, clock))
    clock.t += timedelta(seconds=30)
    return pending


@pytest.mark.parametrize("at,sha1,sha256,sha512", [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826")])
def test_rfc6238_vectors(at, sha1, sha256, sha512):
    from agentpal.identity.totp import totp
    for algorithm, length, expected in [("sha1", 20, sha1), ("sha256", 32, sha256),
                                        ("sha512", 64, sha512)]:
        seed = (b"1234567890" * 7)[:length]
        assert totp(seed, at, digits=8, algorithm=algorithm) == expected


def test_enrollment_step_up_is_separate_short_lived_and_not_forgeable(encryption_key):
    svc, clock, token = identity()
    base = svc.authenticate(token)
    pending = enrolled(svc, token, clock)
    assert pending.secret not in repr(pending)
    with pytest.raises(OwnerAuthError):
        svc.assert_session_active(base.model_copy(update={"mfa_satisfied": True}))
    elevated, info = svc.step_up(token, code(pending.secret, clock))
    assert elevated != token and info.expires_at <= clock() + timedelta(minutes=5)
    assert not svc.authenticate(token).mfa_satisfied
    owner = svc.authenticate(elevated)
    assert owner.mfa_satisfied and owner.auth_method == "owner_session"
    svc.assert_session_active(owner)
    with pytest.raises(IdentityError):
        svc.step_up(token, code(pending.secret, clock))
    clock.t = info.expires_at
    with pytest.raises(OwnerAuthError):
        svc.authenticate(elevated)


@pytest.mark.parametrize("invalidate", ["parent_revoke", "parent_expiry", "owner_disable", "factor_remove"])
def test_step_up_proof_is_bound_to_parent_and_factor(encryption_key, invalidate):
    svc, clock, token = identity()
    pending = enrolled(svc, token, clock)
    elevated, _ = svc.step_up(token, code(pending.secret, clock))
    owner = svc.authenticate(elevated)
    if invalidate == "parent_revoke":
        svc.revoke_token(svc.authenticate(token).session_id)
    elif invalidate == "parent_expiry":
        parent_id = svc.authenticate(token).session_id
        with svc.store.tx() as c:
            c.execute("UPDATE identity_owner_credentials SET expires_at=? WHERE credential_id=?",
                      (clock().isoformat(), parent_id))
    elif invalidate == "owner_disable":
        svc.disable_owner("owner_alice")
    else:
        svc.remove_totp(elevated)
    with pytest.raises(OwnerAuthError):
        svc.authenticate(elevated)
    with pytest.raises(OwnerAuthError):
        svc.assert_session_active(owner)


def test_seed_encryption_key_failure_and_no_seed_or_token_on_disk(tmp_path, encryption_key, monkeypatch):
    svc, clock, token = identity(tmp_path / "id.db")
    pending = enrolled(svc, token, clock)
    elevated, _ = svc.step_up(token, code(pending.secret, clock))
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    for value in (token, elevated, pending.secret):
        assert value.encode() not in raw
    assert base64.b32decode(pending.secret) not in raw
    monkeypatch.setenv("AGENTPAL_MFA_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    with pytest.raises(IdentityError):
        svc.step_up(token, code(pending.secret, clock))
    with pytest.raises(OwnerAuthError):
        svc.authenticate(elevated)
    monkeypatch.delenv("AGENTPAL_MFA_ENCRYPTION_KEY")
    with pytest.raises(OwnerAuthError):
        svc.authenticate(elevated)


def test_lockout_survives_restart_and_pending_reenrollment(tmp_path, encryption_key):
    path = tmp_path / "id.db"
    svc, clock, token = identity(path)
    pending = svc.begin_totp_enrollment(token)
    for _ in range(5):
        with pytest.raises(IdentityError):
            svc.confirm_totp_enrollment(token, "not-a-code")
    svc.store.close()
    svc = OwnerIdentityService(IdentityStore(path), clock=clock)
    with pytest.raises(IdentityError):
        svc.begin_totp_enrollment(token)
    with pytest.raises(IdentityError):
        svc.confirm_totp_enrollment(token, code(pending.secret, clock))
    clock.t += timedelta(minutes=5)
    svc.confirm_totp_enrollment(token, code(pending.secret, clock))


@pytest.mark.parametrize("bad", [None, 123456, "１２３４５６", "12345", "1234567", "", "-12345"])
def test_malformed_codes_are_refused_without_proof(encryption_key, bad):
    svc, clock, token = identity()
    pending = svc.begin_totp_enrollment(token)
    with pytest.raises(IdentityError):
        svc.confirm_totp_enrollment(token, bad)
    assert not svc.authenticate(token).mfa_satisfied
    assert pending.secret not in str(svc.list_tokens("owner_alice"))


def test_enrollment_is_bound_expiring_and_enabled_factor_cannot_be_overwritten(encryption_key):
    svc, clock, token = identity()
    other = svc.issue_token("owner_alice")[0]
    pending = svc.begin_totp_enrollment(token)
    with pytest.raises(IdentityError):
        svc.confirm_totp_enrollment(other, code(pending.secret, clock))
    clock.t += timedelta(minutes=10)
    with pytest.raises(IdentityError):
        svc.confirm_totp_enrollment(token, code(pending.secret, clock))
    pending = enrolled(svc, token, clock)
    with pytest.raises(IdentityError):
        svc.begin_totp_enrollment(token)
    with pytest.raises(IdentityError):
        svc.remove_totp(token)
    with pytest.raises(IdentityError):
        svc.step_up(token, code(pending.secret, lambda: clock() - timedelta(minutes=2)))


def _step_worker(path, token, otp, at, ready, start, output):
    from datetime import datetime
    svc = OwnerIdentityService(IdentityStore(path), clock=lambda: datetime.fromisoformat(at))
    ready.put(True)
    start.wait(15)
    try:
        svc.step_up(token, otp)
        output.put(True)
    except IdentityError:
        output.put(False)
    finally:
        svc.store.close()


def test_simultaneous_processes_accept_one_counter_once(tmp_path, encryption_key):
    path = str(tmp_path / "id.db")
    svc, clock, token = identity(path)
    pending = enrolled(svc, token, clock)
    ctx = mp.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_step_worker, args=(path, token, code(pending.secret, clock),
        clock().isoformat(), ready, start, output)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        assert sum(output.get(timeout=20) for _ in processes) == 1
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
        ready.close()
        output.close()


def test_real_factor_satisfies_existing_http_approval_policy(tmp_path, encryption_key):
    from test_infra_integration_platform import settings_for, bearer
    from agentpal.platform.wiring import build_platform
    from agentpal.api import create_app
    from fastapi.testclient import TestClient
    settings = settings_for(tmp_path / "risk.db")
    deps = build_platform(settings=settings, environ={})
    svc = deps.owner_authenticator
    svc.create_owner("owner_alice")
    token = svc.issue_token("owner_alice")[0]
    clock = Clock(svc.clock())
    svc.clock = clock
    deps.require_mfa_for_approval = True  # existing interface; lead wires the setting
    try:
        app = create_app(service=deps.risk_service, settings=settings, platform=deps)
        with TestClient(app) as client:
            agent = client.post("/v1/agents", headers=bearer(token), json={"name": "MFA bot",
                "capabilities": ["payment.prepare", "owner.request_decision"]}).json()
            response = client.post("/v1/wallets", headers=bearer(token), json={
                "agent_id": agent["agent"]["agent_id"], "currency": "USD",
                "monthly_budget": "100.00", "per_transaction_limit": "10.00"})
            assert response.status_code == 201
            payment = client.post("/v1/payment-requests", headers={**bearer(agent["api_key"]),
                "Idempotency-Key": "mfa-create-0001"}, json={"amount": "1.00", "currency": "USD",
                "merchant_name": "Example", "purpose": "Offline MFA test",
                "request_owner_decision": True}).json()
            path = f'/v1/payment-requests/{payment["request_id"]}/approve'
            body = {"terms_sha256": payment["terms_sha256"]}
            assert client.post(path, json=body, headers=bearer(token)).status_code == 403
            pending = enrolled(svc, token, clock)
            elevated, _ = svc.step_up(token, code(pending.secret, clock))
            assert client.post(path, json=body, headers=bearer(elevated)).status_code == 200
            row = deps.risk_service.store.approvals(payment["request_id"])[0]
            assert row["mfa_satisfied"] == 1 and row["auth_method"] == "owner_session"
    finally:
        deps.close()


def test_cli_enrollment_confirmation_and_step_up_use_hidden_prompts(tmp_path, encryption_key, monkeypatch, capsys):
    from agentpal.identity.__main__ import main
    from agentpal.identity.totp import totp
    from datetime import datetime, timezone
    path = tmp_path / "cli.db"
    svc, _, token = identity(path, clock=Clock(datetime.now(timezone.utc)))
    answers = [token]
    monkeypatch.setattr("getpass.getpass", lambda *args, **kwargs: answers.pop(0))
    assert main(["--db", str(path), "enroll-totp"]) == 0
    secret = capsys.readouterr().out.strip()
    assert len(base64.b32decode(secret)) == 20
    now = datetime.now(timezone.utc).replace(microsecond=0)
    answers[:] = [token, totp(base64.b32decode(secret), int(now.timestamp()))]
    assert main(["--db", str(path), "confirm-totp"]) == 0
    assert secret not in capsys.readouterr().out
    # Use a deterministic next step without waiting, through the service's trusted clock.
    future = now + timedelta(seconds=30)
    monkeypatch.setattr("agentpal.identity.service._now", lambda: future)
    answers[:] = [token, totp(base64.b32decode(secret), int(future.timestamp()))]
    assert main(["--db", str(path), "step-up"]) == 0
    elevated = capsys.readouterr().out.strip()
    svc.clock = lambda: future
    assert svc.authenticate(elevated).mfa_satisfied
    assert token not in elevated


def test_mfa_replay_and_proof_survive_restart(tmp_path, encryption_key):
    path = tmp_path / "id.db"
    svc, clock, token = identity(path)
    pending = enrolled(svc, token, clock)
    otp = code(pending.secret, clock)
    elevated, _ = svc.step_up(token, otp)
    svc.store.close()
    svc = OwnerIdentityService(IdentityStore(path), clock=clock)
    assert svc.authenticate(elevated).mfa_satisfied
    with pytest.raises(IdentityError):
        svc.step_up(token, otp)


def test_step_up_failure_rolls_back_counter_and_credential_atomically(encryption_key):
    import sqlite3
    svc, clock, token = identity()
    pending = enrolled(svc, token, clock)
    with svc.store.tx() as c:
        c.execute("CREATE TRIGGER fail_step BEFORE INSERT ON identity_owner_credentials "
                  "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    otp = code(pending.secret, clock)
    with pytest.raises(sqlite3.IntegrityError):
        svc.step_up(token, otp)
    with svc.store.tx() as c:
        c.execute("DROP TRIGGER fail_step")
    assert svc.authenticate(svc.step_up(token, otp)[0]).mfa_satisfied


@pytest.mark.parametrize("invalid_key", [None, "not-base64", ""])
def test_enrollment_fails_closed_without_valid_encryption_key(monkeypatch, invalid_key):
    svc, _, token = identity()
    if invalid_key is None:
        monkeypatch.delenv("AGENTPAL_MFA_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENTPAL_MFA_ENCRYPTION_KEY", invalid_key)
    with pytest.raises(IdentityError, match="MFA_UNAVAILABLE"):
        svc.begin_totp_enrollment(token)
    assert svc.store.query("SELECT * FROM identity_totp") == []


def test_counter_window_and_enabled_factor_lockout_persist(encryption_key, tmp_path):
    svc, clock, token = identity(tmp_path / "id.db")
    pending = enrolled(svc, token, clock)
    previous = code(pending.secret, lambda: clock() - timedelta(seconds=30))
    with pytest.raises(IdentityError):  # confirmation already consumed that previous step
        svc.step_up(token, previous)
    for _ in range(4):
        with pytest.raises(IdentityError):
            svc.step_up(token, "bad")
    with pytest.raises(IdentityError):
        svc.step_up(token, code(pending.secret, clock))
    clock.t += timedelta(minutes=5)
    elevated, _ = svc.step_up(token, code(pending.secret, clock))
    assert svc.authenticate(elevated).mfa_satisfied


def test_step_up_expiry_cannot_outlive_parent(encryption_key):
    svc, clock, _ = identity()
    parent, info = svc.issue_token("owner_alice", ttl=timedelta(minutes=2))
    pending = enrolled(svc, parent, clock)
    elevated, elevated_info = svc.step_up(parent, code(pending.secret, clock))
    assert elevated_info.expires_at == info.expires_at
    assert svc.authenticate(elevated).mfa_satisfied


def test_membership_change_invalidates_existing_owner_object(encryption_key):
    svc, clock, token = identity()
    pending = enrolled(svc, token, clock)
    elevated, _ = svc.step_up(token, code(pending.secret, clock))
    owner = svc.authenticate(elevated)
    svc.create_org("org_new", "New")
    with svc.store.tx() as c:
        c.execute("UPDATE identity_owners SET org_id='org_new'")
    with pytest.raises(OwnerAuthError):
        svc.assert_session_active(owner)


def test_equal_adjacent_codes_cannot_bypass_counter_replay_guard(monkeypatch):
    import agentpal.identity.totp as primitive
    monkeypatch.setattr(primitive, "totp", lambda *args, **kwargs: "000042")
    # A rare code collision in a newer adjacent counter must not resurrect a used OTP.
    assert primitive.match_counter(b"test", "000042", 60, 1) is None


def test_older_in_window_counter_is_refused_without_changing_counter_and_counts_failure(
        encryption_key, monkeypatch):
    # Published RFC test material makes the three adjacent codes deterministic.
    monkeypatch.setattr("agentpal.identity.service.secrets.token_bytes", lambda length:
                        b"12345678901234567890" if length == 20 else os.urandom(length))
    svc, clock, token = identity()
    pending = svc.begin_totp_enrollment(token)
    svc.confirm_totp_enrollment(token, code(pending.secret, clock))
    clock.t += timedelta(seconds=60)
    newest = code(pending.secret, clock)
    older = code(pending.secret, lambda: clock() - timedelta(seconds=30))
    assert older != newest  # the last-code equality guard cannot reject this value
    svc.step_up(token, newest)
    [before] = svc.store.query("SELECT * FROM identity_totp")
    counter = int(clock().timestamp()) // 30
    assert before["last_counter"] == counter and before["failures"] == 0
    credential_count = len(svc.list_tokens("owner_alice"))

    # N-1 remains inside the accepted clock window, but N has already been consumed.
    with pytest.raises(IdentityError, match="MFA_VERIFICATION_FAILED"):
        svc.step_up(token, older)
    [after] = svc.store.query("SELECT * FROM identity_totp")
    assert after["last_counter"] == counter
    assert after["failures"] == before["failures"] + 1
    assert len(svc.list_tokens("owner_alice")) == credential_count
    assert svc.store.query("SELECT action FROM identity_events ORDER BY id DESC LIMIT 1") == [
        {"action": "totp_verification_failed"}]
