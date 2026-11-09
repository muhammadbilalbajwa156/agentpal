"""Offline CLI/TCP proof with shipped auth/rate limits and no provider credentials."""
from .tcp import TCPWorld


def test_real_cli_owner_bootstrap_and_tenant_approval_gate():
    world = TCPWorld()
    try:
        data, path = world.prepared()
        assert world.tokens["a"].startswith("apk_")
        assert world.call("POST", path + "/approve", "a",
                           json={"terms_sha256": data["terms_sha256"]}).status_code == 403
        assert world.call("POST", path + "/approve", "owner_b",
                           json={"terms_sha256": data["terms_sha256"]}).status_code == 404
        assert world.call("POST", path + "/execute", "a").status_code == 409
        assert world.call("POST", path + "/approve",
                           json={"terms_sha256": data["terms_sha256"]}).status_code == 200
        r = world.call("POST", path + "/execute", "a")
        assert r.status_code == 200 and r.json()["status"] == "provider_pending"
        assert r.json()["is_real_money"] is False
        assert world.call("POST", path + "/execute", "a").status_code == 409
        assert world.allowance()["spent"] == "0.00"
        assert world.allowance()["pending"] == "1.00"
    finally:
        world.close()
