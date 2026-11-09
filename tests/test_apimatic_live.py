"""Real APIMatic calls only when credentials/runtime exist; no successful substitutes."""

import os
import subprocess

import pytest

from apimatic.smoke import run_smoke
from apimatic.workflow import CLI, WorkflowError, remote, require_node


def node24():
    node = os.environ.get("APIMATIC_NODE", "node")
    try:
        require_node(node)
    except WorkflowError:
        pytest.skip("Node 24+ unavailable; APIMatic CLI 2.0.0 requires Node 24+")
    if not CLI.exists():
        pytest.skip("Pinned APIMatic CLI absent; run npm ci --prefix apimatic --cache /tmp/agentpal-npm")
    return node


def test_real_pinned_cli_starts_with_node24():
    node = node24()
    result = subprocess.run([node, str(CLI), "--version"], capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 0
    assert "@apimatic/cli/2.0.0" in result.stdout


def test_real_generated_sdk_against_live_local_server():
    if not os.environ.get("APIMATIC_API_KEY", "").strip():
        pytest.skip("BLOCKED BY CREDENTIALS: APIMATIC_API_KEY absent; generated SDK not available")
    node = node24()
    remote("generate", node=node)
    run_smoke(generated=True)
