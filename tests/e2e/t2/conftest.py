"""T2 isolated real TCP server. Credentials stay in process memory only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from collections import defaultdict, deque
from uuid import uuid4

import httpx
import pytest

from agentpal.identity import OwnerIdentityService
from agentpal.identity.store import IdentityStore

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "sdk/python"))
CAPS = ["wallet.read", "payment.prepare", "owner.request_decision",
        "payment.execute_sandbox", "payout.request", "research.fetch"]


class World:
    def __init__(self, directory, *, live=False, mfa=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity = OwnerIdentityService(IdentityStore(self.directory / "identity.db"))
        self.tokens = {}
        self.agents = {}
        self.wallets = {}
        self.responses = []
        self.calls = defaultdict(deque)
        for name in ("a", "b"):
            self.identity.create_owner("t2_owner_" + name)
            self.tokens["owner_" + name] = self.identity.issue_token("t2_owner_" + name)[0]
        env = dict(os.environ)
        if not live:
            env = {k: v for k, v in env.items() if not k.startswith(
                ("PAYPAL_SANDBOX_", "CHANNEL3_", "KERNEL_"))}
        env.update(AGENTPAL_DB_PATH=str(self.directory / "risk.db"),
                   AGENTPAL_AUTH_MODE="required",
                   AGENTPAL_ALLOWED_HOSTS="127.0.0.1,localhost,::1",
                   AGENTPAL_IDENTITY_DB_PATH=str(self.directory / "identity.db"),
                   AGENTPAL_WALLET_DB_PATH=str(self.directory / "wallet.db"),
                   AGENTPAL_RATE_LIMIT_DB_PATH=str(self.directory / "rate.db"),
                   AGENTPAL_REQUIRE_MFA_FOR_APPROVAL=str(mfa).lower())
        # Keep shipped auth, model and rate-limit settings. Never inherit another DB.
        self.port = free_port()
        self.log = open(self.directory / "server.log", "w+")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agentpal", "serve", "--platform", "--host",
             "127.0.0.1", "--port", str(self.port)], cwd=PROJECT, env=env,
            stdout=self.log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=60,
                                   trust_env=False)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("T2 server exited; inspect redacted server log")
                try:
                    if self.client.get("/v1/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("T2 server readiness timeout")
            for name in ("a", "b"):
                self.register(name, "owner_" + name)
            old = self.register("revoked", "owner_a")
            self.call("DELETE", f"/v1/agents/{old['agent']['agent_id']}/credentials/"
                      + old["key_id"], "owner_a")
            self.tokens["wrong_prefix"] = "legacy_t2_invalid"
        except BaseException:
            self.close()
            raise

    def call(self, method, path, actor=None, *, json=None, headers=None, **kwargs):
        # Pace acceptance traffic below shipped sliding-window quotas. Dedicated
        # limiter attacks use client.request directly and inspect the first 429.
        for bucket, maximum in (("peer", 270), (self.tokens.get(actor, "anonymous"), 55)):
            history = self.calls[bucket]
            now = time.monotonic()
            while history and history[0] < now - 61:
                history.popleft()
            if len(history) >= maximum:
                time.sleep(max(0, history[0] + 61 - now))
            history.append(time.monotonic())
        hs = dict(headers or {})
        if actor:
            hs["Authorization"] = "Bearer " + self.tokens[actor]
        response = self.client.request(method, path, json=json, headers=hs, **kwargs)
        self.responses.append(response)
        return response

    def register(self, name, owner, *, budget="100.00", metadata=None):
        response = self.call("POST", "/v1/agents", owner,
                             json={"name": "T2 " + name, "capabilities": CAPS,
                                   "metadata": metadata or {}})
        assert response.status_code == 201, (response.status_code, response.text)
        data = response.json()
        self.tokens[name] = data["api_key"]
        self.agents[name] = data["agent"]["agent_id"]
        response = self.call("POST", "/v1/wallets", owner, json={
            "agent_id": self.agents[name], "currency": "USD", "monthly_budget": budget,
            "per_transaction_limit": "5.00", "rolling_window_limit": budget})
        assert response.status_code == 201, (response.status_code, response.text)
        self.wallets[name] = response.json()["wallet_id"]
        return data

    def payment(self, actor="a", **updates):
        body = dict(amount="1.00", currency="USD", merchant_name="Northwind",
                    merchant_domain="example.com", purpose="T2 office supplies")
        body.update(updates)
        return self.call("POST", "/v1/payment-requests", actor, json=body,
                         headers={"Idempotency-Key": "t2-" + uuid4().hex})

    def close(self):
        self.client.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.log.close()
        self.identity.store.close()


@pytest.fixture(scope="session")
def world():
    instance = World(Path("/tmp/e2e-t2") / ("offline-" + uuid4().hex))
    try:
        yield instance
    finally:
        instance.close()


def free_port() -> int:
    """An unused loopback port, so parallel test runs never share a server (E2E-F1)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
