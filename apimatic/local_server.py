"""Disposable real CLI server with strict identity/lifecycle and a local simulator."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .workflow import ROOT, WorkflowError


@dataclass(frozen=True)
class LocalServer:
    base_url: str
    owner_token: str = field(repr=False)
    process: subprocess.Popen = field(repr=False)


def server_environment(scratch: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTPAL_", "PAYPAL_", "APIMATIC_"))}
    env.update({
        "AGENTPAL_ENV": "development", "AGENTPAL_AUTH_MODE": "required",
        "AGENTPAL_DB_PATH": str(scratch / "ledger.db"),
        "AGENTPAL_IDENTITY_DB_PATH": str(scratch / "identity.db"),
        "AGENTPAL_WALLET_DB_PATH": str(scratch / "wallet.db"),
        "AGENTPAL_ANALYZER_CHAIN": "heuristic",
        "AGENTPAL_POLICY_PATH": str(ROOT / "config/risk_policy.json"),
        "AGENTPAL_SPEND_POLICY_PATH": str(ROOT / "config/spend_policy.json"),
        "AGENTPAL_ALLOWED_HOSTS": "127.0.0.1,localhost,::1",
        "AGENTPAL_LOG_LEVEL": "ERROR", "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    })
    return env


def wait_ready(process: subprocess.Popen, base_url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=0.5) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkflowError("Local serve --platform process exited during startup")
            try:
                response = client.get(base_url + "/v1/health")
                if (response.status_code == 200 and response.json().get("status") == "ok"
                        and response.json().get("is_real_money") is False):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.05)
    raise WorkflowError("Timed out waiting for local serve --platform")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def local_server():
    from agentpal.identity import OwnerIdentityService
    from agentpal.identity.store import IdentityStore

    with tempfile.TemporaryDirectory(prefix="agentpal-sdk-smoke-") as temporary:
        scratch = Path(temporary)
        identity = OwnerIdentityService(IdentityStore(scratch / "identity.db"))
        try:
            identity.create_owner("owner_apimatic_smoke")
            owner_token, _ = identity.issue_token("owner_apimatic_smoke")
        finally:
            identity.store.close()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        # One-time keys live in memory only. Suppress logs rather than risk persisting them.
        process = subprocess.Popen(
            [sys.executable, "-m", "agentpal", "serve", "--platform", "--host", "127.0.0.1",
             "--port", str(port)], cwd=ROOT, env=server_environment(scratch),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready(process, base_url)
            yield LocalServer(base_url, owner_token, process)
        finally:
            stop(process)
