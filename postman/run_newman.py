"""Run pinned Newman against a disposable, authenticated loopback CLI server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
POSTMAN = ROOT / "postman"


def redact(value: str, credentials: list[str]) -> str:
    """Newman exports request headers and variables; scrub before persisting/printing."""
    for credential in credentials:
        value = value.replace(credential, "[REDACTED]")
    return value


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_ready(server: subprocess.Popen, base_url: str, timeout: float = 20) -> None:
    # Ignore inherited proxy configuration: this request must stay on loopback.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("local server exited before /health became ready")
        try:
            with opener.open(base_url + "/health", timeout=0.5) as response:
                health = json.load(response)
                if (health.get("status") == "ok"
                        and health.get("authentication") == "required"
                        and health.get("payment_execution_enabled") is False):
                    return
        except (URLError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for authenticated local /health")


def _stop(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _provision_v1(base_url: str, owner_key: str) -> dict[str, str]:
    """Provision through the real HTTP API; only owner bootstrap is out of band."""
    opener = build_opener(ProxyHandler({}))

    def post(path, body):
        request = Request(base_url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + owner_key, "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        except (URLError, ValueError):
            raise RuntimeError("v1 HTTP provisioning failed; response suppressed") from None

    result = {}
    for label in ("a", "b"):
        agent = post("/v1/agents", {"name": "postman-" + label, "capabilities": [
            "wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox"]})
        agent_id = agent["agent"]["agent_id"]
        # Keep even newly issued credentials available for redaction if provisioning
        # a later resource fails. This helper never prints or persists response bodies.
        result[f"agent_{label}_key"] = agent["api_key"]
        result[f"agent_{label}_id"] = agent_id
        post("/v1/wallets", {"agent_id": agent_id, "currency": "USD",
             "monthly_budget": "100.00", "per_transaction_limit": "10.00"})
    return result


def run(mode: str = "legacy", reports_dir: Path = POSTMAN / "reports") -> int:
    if mode == "v1" and not (ROOT / "docs/api/openapi.json").is_file():
        print("PENDING v1: skipped; docs/api/openapi.json is absent locally.")
        return 0
    if not shutil.which("node") or not shutil.which("npx"):
        raise RuntimeError("BLOCKED: node and npx are required (Newman 6.2.3)")
    # An isolated environment prevents pytest's legacy dev-auth fixture or operator
    # overrides from disabling authentication/changing DB/model paths in this process.
    child_env = {key: value for key, value in os.environ.items()
                 if not key.startswith(("AGENTPAL_", "PAYPAL_SANDBOX_"))
                 and key != "POSTMAN_API_KEY"}
    child_env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    child_env["no_proxy"] = child_env["NO_PROXY"]
    with tempfile.TemporaryDirectory(prefix="agentpal-npm-") as npm_cache:
        installed = subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", npm_cache],
            cwd=POSTMAN, env=child_env, text=True, capture_output=True, timeout=120,
        )
    if installed.returncode:
        # npm errors can contain registry credentials from local configuration.
        raise RuntimeError("BLOCKED: npm ci failed; verify registry access and package-lock.json")
    print("npm ci: pinned newman@6.2.3 installed.")
    from .generate import build_collection, legacy_spec

    spec = (legacy_spec() if mode == "legacy" else
            json.loads((ROOT / "docs/api/openapi.json").read_text()))
    collection = build_collection(spec, mode=mode)
    folder = "LEGACY security" if mode == "legacy" else "V1 security"
    # Only explicit security scenarios execute. Generated API examples are documentation.
    collection["item"] = [item for item in collection["item"] if item["name"] == folder]
    if not collection["item"]:
        raise RuntimeError(f"BLOCKED: collection has no {folder} folder")
    credentials = {key: "apt_" + secrets.token_urlsafe(40)
                   for key in ("agent_a_key", "agent_b_key", "invalid_key")}
    base_url = f"http://127.0.0.1:{_free_port()}"
    with tempfile.TemporaryDirectory(prefix="agentpal-newman-") as temporary:
        scratch = Path(temporary)
        token_file = scratch / "tokens.json"
        token_file.write_text(json.dumps({"tokens": [
            {"id": label, "role": "agent", "agent_id": agent,
             "sha256": hashlib.sha256(credentials[key].encode()).hexdigest()}
            for label, agent, key in (("postman-a", "agent_demo_001", "agent_a_key"),
                                      ("postman-b", "agent_demo_002", "agent_b_key"))
        ]}))
        token_file.chmod(0o600)
        child_env.update({
            "AGENTPAL_ENV": "development", "AGENTPAL_AUTH_MODE": "required",
            "AGENTPAL_API_TOKENS_FILE": str(token_file),
            "AGENTPAL_DB_PATH": str(scratch / "assessments.db"),
            "AGENTPAL_ANALYZER_CHAIN": "heuristic",
            "AGENTPAL_SPEND_POLICY_PATH": str(ROOT / "config/spend_policy.json"),
            "AGENTPAL_ALLOWED_HOSTS": "127.0.0.1,localhost,::1",
            "AGENTPAL_RATE_LIMIT_PER_TOKEN": "1000",
            "AGENTPAL_RATE_LIMIT_PER_PEER": "1000",
        })
        if mode == "v1":
            from agentpal.identity import OwnerIdentityService
            from agentpal.identity.store import IdentityStore

            identity_path = scratch / "identity.db"
            identity = OwnerIdentityService(IdentityStore(identity_path))
            try:
                identity.create_owner("owner_postman")
                credentials["owner_key"], _ = identity.issue_token("owner_postman")
            finally:
                identity.store.close()
            child_env["AGENTPAL_IDENTITY_DB_PATH"] = str(identity_path)
            child_env["AGENTPAL_WALLET_DB_PATH"] = str(scratch / "wallet.db")
        # Logs stay private in scratch and are deleted even on exceptions. Never dump
        # raw server/Newman output: failure messages may include Authorization headers.
        with (scratch / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "agentpal", "serve", "--host", "127.0.0.1",
                 "--port", str(base_url.rsplit(":", 1)[1]),
                 *(["--platform"] if mode == "v1" else [])],
                cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                _wait_ready(server, base_url)
                print("Local server ready: authentication required; payment execution disabled.")
                if mode == "v1":
                    credentials.update(_provision_v1(base_url, credentials["owner_key"]))
                    print("V1 ready: real owner identity, agents and virtual wallets; local simulator.")
                for variable in collection["variable"]:
                    if variable["key"] in credentials:
                        variable["value"] = credentials[variable["key"]]
                    elif variable["key"] == "base_url":
                        variable["value"] = base_url
                runtime_collection = scratch / "runtime.collection.json"
                runtime_collection.write_text(json.dumps(collection))
                runtime_collection.chmod(0o600)
                result = subprocess.run(
                    [str(POSTMAN / "node_modules/.bin/newman"), "run", str(runtime_collection),
                     "--reporters", "cli,junit,json", "--reporter-junit-export",
                     str(scratch / f"{mode}.xml"), "--reporter-json-export",
                     str(scratch / f"{mode}.json"), "--timeout-request", "5000",
                     "--timeout-script", "5000", "--color", "off"],
                    cwd=ROOT, env=child_env, text=True, capture_output=True, timeout=90,
                )
                print(redact(result.stdout + result.stderr, list(credentials.values())))
                reports_dir.mkdir(parents=True, exist_ok=True)
                for suffix in ("xml", "json"):
                    report = scratch / f"{mode}.{suffix}"
                    if report.exists():
                        target = reports_dir / report.name
                        target.write_text(redact(report.read_text(), list(credentials.values())))
                        target.chmod(0o600)
                print(f"Newman exit code: {result.returncode}; sanitized JUnit + JSON reports written.")
                return result.returncode
            finally:
                _stop(server)
                print("Local server stopped.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("legacy", "v1"), default="legacy")
    parser.add_argument("--reports-dir", type=Path, default=POSTMAN / "reports")
    args = parser.parse_args(argv)
    try:
        return run(args.mode, args.reports_dir)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        # OSError filenames/TimeoutExpired command contain only generated paths, no keys.
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
