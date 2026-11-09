"""Own CLI/TCP server on 8205; secrets and raw responses stay in memory."""
from collections import defaultdict, deque
from contextlib import contextmanager
import json
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
BASE = "http://127.0.0.1:8205"
CAPS = ["wallet.read", "payment.prepare", "owner.request_decision",
        "payment.execute_sandbox", "payout.request"]


class Secret(str):
    def __repr__(self):
        return "<credential hidden>"


class Server:
    def __init__(self, directory, *, live=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("AGENTPAL_", "PAYPAL_", "CHANNEL3_", "KERNEL_", "F1_"))}
        if live:
            self.env.update({k: v for k, v in os.environ.items()
                             if k.startswith(("PAYPAL_SANDBOX_", "CHANNEL3_"))})
        self.env.update(AGENTPAL_DB_PATH=str(self.directory / "ledger.db"),
                        AGENTPAL_AUTH_MODE="required", AGENTPAL_ENV="development",
                        AGENTPAL_ANALYZER_CHAIN="heuristic",
                        AGENTPAL_ALLOWED_HOSTS="127.0.0.1,localhost,::1")
        self.tokens = []
        self.owners = {}
        self.logs = deque(maxlen=2000)
        self.history = defaultdict(deque)
        self.client = httpx.Client(base_url=BASE, trust_env=False, timeout=65)
        self.proc = None

    def __repr__(self):
        return "<F1 private CLI/TCP harness>"

    def owner(self, name):
        __tracebackhide__ = True
        result = subprocess.run([sys.executable, "-m", "agentpal", "new-owner", name,
                                 "--name", "F1 fresh owner"], cwd=ROOT, env=self.env,
                                capture_output=True, timeout=15)
        if result.returncode or not result.stdout.strip().startswith(b"apo_"):
            raise AssertionError("documented new-owner command failed; output suppressed")
        token = Secret(result.stdout.decode().strip())
        self.tokens.append(token)
        self.owners[name] = token
        return token

    def start(self):
        __tracebackhide__ = True
        # Refuse an occupied port before starting or sending any traffic.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", 8205))
        self.proc = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--platform",
                                      "--host", "127.0.0.1", "--port", "8205"],
                                     cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        def read():
            for line in self.proc.stdout:
                self.logs.append(line)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        for _ in range(200):
            if self.proc.poll() is not None:
                raise AssertionError("own server exited before readiness; logs suppressed")
            try:
                if self.client.get("/v1/health").status_code == 200:
                    return self
            except httpx.ConnectError:
                pass
            time.sleep(.05)
        raise AssertionError("own server readiness timed out")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.reader.join(timeout=2)
            self.proc.stdout.close()
            self.proc = None

    def restart(self):
        self.stop()
        self.start()

    def call(self, method, path, token=None, *, body=None, key=None, expected=(200, 201, 204)):
        __tracebackhide__ = True
        # Keep bounded journeys below the shipped quotas without changing policy.
        for bucket, limit in (("peer", 250), (token, 50)):
            history = self.history[bucket]
            while history and history[0] < time.monotonic() - 61:
                history.popleft()
            if len(history) >= limit:
                time.sleep(max(0, history[0] + 61 - time.monotonic()))
            history.append(time.monotonic())
        headers = {"Authorization": "Bearer " + token} if token else {}
        if key:
            headers["Idempotency-Key"] = key
        response = self.client.request(method, path, headers=headers, json=body)
        if response.status_code not in expected:
            raise AssertionError(f"F1 {method} operation returned HTTP {response.status_code}; body suppressed")
        return response.json() if response.content else None

    def agent(self, owner, *, currency="USD", budget="100.00", limit="10.00", caps=CAPS):
        __tracebackhide__ = True
        reg = self.call("POST", "/v1/agents", owner,
                        body={"name": "F1 fresh developer agent", "capabilities": caps})
        token = Secret(reg["api_key"])
        self.tokens.append(token)
        aid = reg["agent"]["agent_id"]
        wallet = self.call("POST", "/v1/wallets", owner, body={"agent_id": aid,
            "currency": currency, "monthly_budget": budget, "per_transaction_limit": limit})
        return aid, token, wallet["wallet_id"]

    def payment(self, token, *, key=None, **options):
        return self.call("POST", "/v1/payment-requests", token, key=key or uuid4().hex,
                         body={"amount": "1.00", "currency": "USD", "merchant_name": "F1 sandbox shop",
                               "merchant_domain": "example.com", "purpose": "F1 sandbox office purchase", **options})

    def rows(self, sql, params=()):
        with sqlite3.connect(f"file:{self.directory / 'ledger.db'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql, params)]

    def secrets_absent(self):
        secrets = [str(s).encode() for s in self.tokens]
        secrets += [v.encode() for k, v in os.environ.items() if v and (
            k in {"PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET",
                  "PAYPAL_SANDBOX_BUYER_EMAIL", "PAYPAL_SANDBOX_BUYER_PASSWORD", "CHANNEL3_API_KEY"})]
        logs = b"".join(self.logs)
        assert not any(secret in logs for secret in secrets), "credential appeared in server logs"
        for path in self.directory.glob("*.db*"):
            if path.is_file():
                content = path.read_bytes()
                assert not any(str(s).encode() in content for s in self.tokens), "plaintext credential persisted"


@contextmanager
def server(directory, *, live=False):
    instance = Server(directory, live=live)
    try:
        instance.owner("f1_owner_a")
        instance.owner("f1_owner_b")
        instance.start()
        yield instance
        instance.secrets_absent()
    finally:
        instance.stop()
        instance.client.close()


def save_evidence(name, data):
    """Callers supply only explicit safe scalar fields, never raw responses."""
    target = Path("/tmp/m8-f1") / name
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
