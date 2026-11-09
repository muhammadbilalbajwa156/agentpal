"""Legacy reads must use authenticated per-agent request identity."""
from fastapi.testclient import TestClient

from agentpal.api import create_app
from test_sec_auth import TOKEN_A, TOKEN_B, TOKEN_OPS, auth, body, service, settings, token_file  # noqa: F401


def test_each_agent_reads_its_own_shared_id(token_file):  # noqa: F811
    svc = service()
    with TestClient(create_app(service=svc, settings=settings(token_file))) as c:
        for token, agent in [(TOKEN_A, "agent_demo_001"), (TOKEN_B, "agent_demo_002")]:
            assert c.post("/assess", json=body("req_shared_001", agent),
                          headers=auth(token)).status_code == 200
        for token, agent in [(TOKEN_A, "agent_demo_001"), (TOKEN_B, "agent_demo_002")]:
            response = c.get("/assessments/req_shared_001", headers=auth(token))
            assert response.status_code == 200
            assert response.json()["agent_id"] == agent
        assert c.get("/assessments/req_shared_001", headers=auth(TOKEN_OPS)).status_code == 404


def test_real_agent_rotation_and_owner_revocation_survive_independent_reopen(tmp_path):
    from datetime import datetime, timedelta, timezone
    from agentpal.identity import IdentityStore, OwnerIdentityService
    from agentpal.infra_contracts import OwnerAuthError
    from agentpal.platform.wiring import build_platform
    from test_identity_owner_auth import Clock
    from test_infra_integration_platform import settings_for, bearer
    import pytest
    clock = Clock(datetime.now(timezone.utc))
    config = settings_for(tmp_path / "risk.db")
    deps = build_platform(settings=config, environ={})
    deps.risk_service.clock = clock
    deps.owner_authenticator.clock = clock
    identity = deps.owner_authenticator
    identity.create_owner("owner_durable")
    token, info = identity.issue_token("owner_durable", ttl=timedelta(minutes=10))
    owner = identity.authenticate(token)
    with TestClient(create_app(service=deps.risk_service, settings=config, platform=deps)) as client:
        agent = client.post("/v1/agents", json={"name": "Durable bot", "capabilities": ["wallet.read"]},
                            headers=bearer(token)).json()
        agent_id, old_key = agent["agent"]["agent_id"], agent["api_key"]
        rotated = client.post(f"/v1/agents/{agent_id}/credentials", json={"overlap_seconds": 2},
                              headers=bearer(token)).json()
        assert client.get("/v1/me", headers=bearer(old_key)).status_code == 200
        clock.t += timedelta(seconds=2)
        assert client.get("/v1/me", headers=bearer(old_key)).status_code == 401
        assert client.get("/v1/me", headers=bearer(rotated["api_key"])).status_code == 200
    identity_path = identity.store.path
    deps.close()
    deps = build_platform(settings=config, environ={})
    deps.risk_service.clock = clock
    deps.owner_authenticator.clock = clock
    try:
        with TestClient(create_app(service=deps.risk_service, settings=config, platform=deps)) as client:
            assert client.get("/v1/me", headers=bearer(old_key)).status_code == 401
            assert client.get("/v1/me", headers=bearer(rotated["api_key"])).status_code == 200
            assert client.delete(f'/v1/agents/{agent_id}/credentials/{rotated["key_id"]}',
                                 headers=bearer(token)).status_code == 204
            assert client.get("/v1/me", headers=bearer(rotated["api_key"])).status_code == 401
            other = OwnerIdentityService(IdentityStore(identity_path), clock=clock)
            other.revoke_token(info.credential_id)
            other.store.close()
            assert client.get("/v1/agents", headers=bearer(token)).status_code == 401
            with pytest.raises(OwnerAuthError):
                deps.owner_authenticator.assert_session_active(owner)
    finally:
        deps.close()
