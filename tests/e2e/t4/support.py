"""External-developer HTTP harness; credentials live in memory only."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[3]
BASE = "http://127.0.0.1:8104"
CAPABILITIES = ["wallet.read", "payment.prepare", "owner.request_decision",
                "payment.execute_sandbox", "payout.request"]


def rows(path, sql, args=()):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


def environment(directory, *, live=False, token_limit=5000):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTPAL_", "PAYPAL_", "CHANNEL3_", "KERNEL_"))}
    if live:
        env.update({k: v for k, v in os.environ.items()
                    if k.startswith(("PAYPAL_SANDBOX_", "CHANNEL3_"))})
    env.update({"AGENTPAL_DB_PATH": str(directory / "ledger.db"),
                "AGENTPAL_IDENTITY_DB_PATH": str(directory / "identity.db"),
                "AGENTPAL_WALLET_DB_PATH": str(directory / "wallet.db"),
                "AGENTPAL_ANALYZER_CHAIN": "heuristic", "AGENTPAL_AUTH_MODE": "required",
                "AGENTPAL_ENV": "development", "AGENTPAL_ALLOWED_HOSTS": "127.0.0.1,localhost",
                "AGENTPAL_RATE_LIMIT_PER_TOKEN": str(token_limit),
                "AGENTPAL_RATE_LIMIT_PER_PEER": "20000", "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    return env


class Server:
    def __init__(self, directory, *, live=False, token_limit=5000, port=None, fake=False):
        from agentpal.identity import OwnerIdentityService
        from agentpal.identity.store import IdentityStore
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.env = environment(self.directory, live=live, token_limit=token_limit)
        port = port or free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.port, self.fake = port, fake
        self.process = None
        self.logs = bytearray()
        self.owner_id = "t4_owner_" + uuid4().hex
        identity = OwnerIdentityService(IdentityStore(self.directory / "identity.db"))
        try:
            identity.create_owner(self.owner_id)
            self.owner, _ = identity.issue_token(self.owner_id)
        finally:
            identity.store.close()
        self.client = httpx.Client(base_url=self.base, trust_env=False, timeout=40)

    def start(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", self.port))
            except OSError:
                raise RuntimeError("T4 port already occupied; refusing to reuse another server") from None
        command = ([sys.executable, str(Path(__file__).with_name("fake_server.py"))]
                   if self.fake else [sys.executable, "-m", "agentpal"])
        command += ["serve", "--platform", "--host", "127.0.0.1", "--port", str(self.port)]
        self.process = subprocess.Popen(command, cwd=ROOT, env=self.env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        process = self.process

        def drain():
            for line in process.stdout:
                self.logs.extend(line)
        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("T4 CLI startup failed; raw logs retained in memory only")
            try:
                response = self.client.get("/v1/health", timeout=.3)
                if response.status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(.03)
        self.stop(kill=True)
        raise RuntimeError("T4 server readiness timeout")

    def stop(self, *, kill=False):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill() if kill else self.process.terminate()
                try:
                    self.process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.reader.join(timeout=2)
            self.process.stdout.close()

    def restart(self):
        self.stop(kill=True)
        return self.start()

    def close(self):
        self.stop()
        self.client.close()

    def call(self, method, path, *, token=None, expected=(200,), key=None, **kwargs):
        headers = {"Authorization": "Bearer " + (token or self.owner)}
        if key is not None:
            headers["Idempotency-Key"] = key
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code not in expected:
            # Never show a registration response or arbitrary server/provider body.
            code = "non_json"
            try:
                code = response.json().get("error", "none")
            except ValueError:
                pass
            raise AssertionError(f"T4 {method} operation: HTTP {response.status_code}; code={code}")
        return response

    def agent(self, *, budget="100.00", limit="5.00"):
        registered = self.call("POST", "/v1/agents", expected=(201,),
            json={"name": "T4 external acceptance agent", "capabilities": CAPABILITIES}).json()
        aid, credential = registered["agent"]["agent_id"], registered["api_key"]
        wallet = self.call("POST", "/v1/wallets", expected=(201,), json={"agent_id": aid,
            "currency": "USD", "monthly_budget": budget, "per_transaction_limit": limit}).json()
        return aid, credential, wallet

    def payment(self, credential, *, amount="1.00", key=None, **extra):
        return self.call("POST", "/v1/payment-requests", token=credential,
            expected=(200, 201), key=key or uuid4().hex,
            json={"amount": amount, "currency": "USD", "merchant_name": "T4 sandbox Test Store",
                  "merchant_domain": "example.com", "purpose": "T4 sandbox acceptance", **extra}).json()

    def approve(self, payment, *, payout=False):
        path = "/v1/payout-requests/" if payout else "/v1/payment-requests/"
        return self.call("POST", path + payment["request_id"] + "/approve",
                         json={"terms_sha256": payment["terms_sha256"]}).json()

    def ledger(self, sql, args=()):
        return rows(self.directory / "ledger.db", sql, args)

    def wallet_rows(self, sql, args=()):
        return rows(self.directory / "wallet.db", sql, args)

    def allowance(self, credential):
        return self.call("GET", "/v1/me/wallet", token=credential).json()["wallets"][0]["allowance"]


def free_port() -> int:
    """An unused loopback port, so parallel test runs never share a server (E2E-F1)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
