"""Exercise the preserved SDK and, when present, the real APIMatic-generated SDK."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from .local_server import LocalServer, local_server
from .workflow import ROOT, SDK, WorkflowError, verify_artifact


async def existing_sdk_flow(server: LocalServer) -> dict:
    sys.path.insert(0, str(ROOT / "sdk/python"))
    from agentpal_sdk import AgentClient, ConflictError, OwnerClient, PermissionDeniedError

    async with OwnerClient(base_url=server.base_url, credential=server.owner_token) as owner:
        registration = await owner.register_agent(name="apimatic-smoke", capabilities=[
            "wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox"])
        agent_id, key = registration["agent"]["agent_id"], registration["api_key"]
        await owner.create_wallet(agent_id=agent_id, currency="USD", monthly_budget="100.00",
                                  per_transaction_limit="10.00")
        async with AgentClient(base_url=server.base_url, credential=key) as agent:
            assert (await agent.get_me())["agent_id"] == agent_id
            wallets = await agent.get_wallet()
            assert wallets["is_real_money"] is False
            assert Decimal(wallets["wallets"][0]["allowance"]["spent"]) == 0
            body = {"amount": "1.00", "currency": "USD", "merchant_name": "Example Hosting",
                    "merchant_domain": "example.com", "purpose": "offline simulator verification"}
            payment = await agent.create_payment_request(**body, idempotency_key="sdk-smoke-0001")
            replay = await agent.create_payment_request(**body, idempotency_key="sdk-smoke-0001")
            assert payment == replay
            try:
                await agent.create_payment_request(**{**body, "amount": "2.00"},
                                                   idempotency_key="sdk-smoke-0001")
            except ConflictError as exc:
                assert exc.code == "idempotency_key_reused"
            else:
                raise AssertionError("different-body replay was accepted")
            # Authenticated test-owner decision, bound to the exact returned terms.
            await owner.approve_payment_request(payment["request_id"],
                                                terms_sha256=payment["terms_sha256"])
            result = await agent.execute_payment_request(payment["request_id"])
            assert result["is_real_money"] is False
            assert result["status"] in {"submitted_to_provider", "provider_pending"}
            assert (await owner.get_reconciliation(payment["request_id"]))["status"] != \
                "provider_confirmed"
            # The generated SDK will also receive each credential in a separate client.
            try:
                await owner._request("GET", "me")
            except PermissionDeniedError:
                pass
            else:
                raise AssertionError("owner credential acted as agent")
        return {"base_url": server.base_url, "owner_token": server.owner_token,
                "agent_key": key, "agent_id": agent_id}


def generated_probe(payload: dict, directory: Path = SDK) -> None:
    """Install into an isolated venv; never replace/install over the handwritten SDK."""
    verify_artifact(directory)
    projects = list(directory.rglob("pyproject.toml"))
    if len(projects) != 1:
        raise WorkflowError("Expected exactly one generated Python project")
    # No credentials inherited by pip; runtime credentials are delivered only on stdin.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIMATIC_", "PAYPAL_", "AGENTPAL_"))}
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="agentpal-generated-venv-") as temporary:
        venv = Path(temporary) / "venv"
        steps = [[sys.executable, "-m", "venv", str(venv)],
                 [str(venv / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                  str(projects[0].parent)]]
        for command in steps:
            try:
                result = subprocess.run(command, env=env, capture_output=True, timeout=180,
                                        check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise WorkflowError("Generated SDK isolated installation could not finish") from None
            if result.returncode:
                raise WorkflowError("Generated SDK isolated installation failed")
        result = subprocess.run(
            [str(venv / "bin/python"), str(ROOT / "apimatic/generated_probe.py"),
             str(projects[0].parent)], input=json.dumps(payload), env=env,
            capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise WorkflowError("Generated SDK live HTTP probe failed (raw output suppressed)")
        if json.loads(result.stdout) != {"generated_sdk_http_reads": 3}:
            raise WorkflowError("Generated SDK probe did not complete its HTTP reads")


def run_smoke(*, generated: bool = True) -> None:
    if generated:
        verify_artifact()  # fail before provisioning if credentials have not produced an SDK
    with local_server() as server:
        payload = asyncio.run(existing_sdk_flow(server))
        if generated:
            generated_probe(payload)
