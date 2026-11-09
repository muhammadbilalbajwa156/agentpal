"""The executable demo is offline unless live mode is explicitly selected."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest


@pytest.mark.parametrize("timeout", [False, True])
def test_demo_runs_real_services_without_network(monkeypatch, timeout):
    from agentpal.demo.paypal_flow import run_flow
    def unexpected_network(*args, **kwargs):
        raise AssertionError("offline demo attempted network I/O")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_network)
    report = asyncio.run(run_flow(simulate_capture_timeout=timeout))
    assert report["mode"] == "offline_mocked_paypal"
    assert report["status"] == "provider_confirmed"
    assert report["verified_outcomes"] == 1
    assert report["capture_posts"] == 1
    assert report["webhook_deliveries"] == [200, 200]
    assert report["states"][0] == "awaiting_approval"
    assert "reconciliation_required" in report["states"] if timeout else True


def test_default_demo_command_is_offline_even_with_partial_credentials():
    env = {**os.environ, "PAYPAL_SANDBOX_CLIENT_ID": "partial-must-not-select-live"}
    env.pop("PAYPAL_SANDBOX_CLIENT_SECRET", None)
    env.pop("PAYPAL_SANDBOX_SECRET", None)
    result = subprocess.run([sys.executable, "-m", "agentpal.demo.paypal_flow"], env=env,
                            cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["mode"] == "offline_mocked_paypal"
    assert report["status"] == "provider_confirmed"


def test_live_demo_missing_credentials_is_explicitly_blocked():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAYPAL_SANDBOX_")}
    result = subprocess.run([sys.executable, "-m", "agentpal.demo.paypal_flow", "--live"],
                            env=env, cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert result.returncode == 2
    assert "BLOCKED BY CREDENTIALS" in result.stdout


def test_live_buyer_approval_cannot_be_assumed(monkeypatch):
    from agentpal.demo.paypal_flow import require_buyer_approval
    monkeypatch.setattr("builtins.input", lambda: "")
    assert require_buyer_approval("https://www.sandbox.paypal.com/checkoutnow") is False
    monkeypatch.setattr("builtins.input", lambda: "APPROVED")
    assert require_buyer_approval("https://www.sandbox.paypal.com/checkoutnow") is True
