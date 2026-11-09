"""H2 owns loopback port 8202 and isolated CLI-created databases, never shared servers."""
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx

from .conftest import PROJECT, World


class TCPWorld(World):
    def __init__(self, *, live=False):
        self.directory = Path("/tmp/m8-h2") / (("live-" if live else "offline-") + uuid4().hex)
        self.directory.mkdir(parents=True, mode=0o700)
        self.port_lock = Path("/tmp/m8-h2/port8202.lock").open("a")
        try:
            fcntl.flock(self.port_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.port_lock.close()
            raise RuntimeError("H2 port 8202 is already owned by another H2 test run") from None
        # Refuse a collision before starting or sending ANY request to this port.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 8202))
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ("AGENTPAL_", "CHANNEL3_", "KERNEL_", "PAYPAL_"))}
        if live:
            # Only sandbox app settings are needed by the server; buyer secrets stay
            # in the independent labelled browser harness, outside the agent API.
            env.update({k: v for k, v in os.environ.items() if k.startswith("PAYPAL_SANDBOX_")
                        and not k.startswith("PAYPAL_SANDBOX_BUYER_")})
        env.update(AGENTPAL_DB_PATH=str(self.directory / "risk.db"),
                   AGENTPAL_IDENTITY_DB_PATH=str(self.directory / "identity.db"),
                   AGENTPAL_WALLET_DB_PATH=str(self.directory / "wallet.db"),
                   AGENTPAL_RATE_LIMIT_DB_PATH=str(self.directory / "rate.db"),
                   AGENTPAL_ANALYZER_CHAIN="heuristic", AGENTPAL_AUTH_MODE="required",
                   AGENTPAL_ALLOWED_HOSTS="127.0.0.1", AGENTPAL_REQUIRE_MFA_FOR_APPROVAL="false")
        self.tokens, self.agents, self.wallets, self.keys = {}, {}, {}, {}
        for name in ("owner_a", "owner_b"):
            result = subprocess.run([sys.executable, "-m", "agentpal", "new-owner", "h2_" + name],
                                    cwd=PROJECT, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 or not result.stdout.strip().startswith("apo_"):
                raise RuntimeError("H2 CLI owner bootstrap failed; output withheld")
            self.tokens[name] = result.stdout.strip()
        self.log = (self.directory / "server.log").open("w")
        self.process = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--platform",
            "--host", "127.0.0.1", "--port", "8202"], cwd=PROJECT, env=env,
            stdout=self.log, stderr=self.log)
        self.client = httpx.Client(base_url="http://127.0.0.1:8202", timeout=60, trust_env=False)
        try:
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError("H2 server exited; private log retained")
                try:
                    if self.client.get("/v1/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("H2 server readiness deadline")
            self.register("a")
            self.register("b", owner="owner_b")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.client.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.log.close()
        self.port_lock.close()
