"""Existing SDK over TCP against python -m agentpal serve --platform, not ASGI."""

import asyncio
import subprocess

import pytest

from apimatic import local_server as local
from apimatic.smoke import existing_sdk_flow
from apimatic.workflow import WorkflowError


def test_existing_sdk_real_cli_server_and_cleanup():
    with local.local_server() as server:
        process = server.process
        payload = asyncio.run(existing_sdk_flow(server))
        assert payload["agent_id"].startswith("agt_")
        assert payload["agent_key"] not in repr(server)
        assert server.owner_token not in repr(server)
    assert process.poll() is not None


def test_server_environment_cannot_inherit_payment_credentials(monkeypatch, tmp_path):
    for name in ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET", "PAYPAL_CLIENT_ID",
                 "APIMATIC_API_KEY", "AGENTPAL_API_TOKENS_FILE", "AGENTPAL_OWNER_TOKEN"):
        monkeypatch.setenv(name, "test-only-do-not-inherit")
    monkeypatch.setenv("AGENTPAL_AUTH_MODE", "disabled_insecure_dev")
    env = local.server_environment(tmp_path)
    assert "test-only-do-not-inherit" not in env.values()
    assert env["AGENTPAL_AUTH_MODE"] == "required"
    assert env["AGENTPAL_ANALYZER_CHAIN"] == "heuristic"
    assert len({env[k] for k in ("AGENTPAL_DB_PATH", "AGENTPAL_IDENTITY_DB_PATH",
                                "AGENTPAL_WALLET_DB_PATH")}) == 3


def test_readiness_failure_stops_the_real_child(monkeypatch):
    processes = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    def fail(*args, **kwargs):
        raise WorkflowError("injected readiness failure")

    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.setattr(local, "wait_ready", fail)
    with pytest.raises(WorkflowError, match="readiness"):
        with local.local_server():
            pytest.fail("unready server escaped")
    assert processes[0].poll() is not None
