"""Pinned CLI invocation, credential gates and provenance. No persisted CLI login."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from agentpal.platform.openapi import DEFAULT_PATH, render

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "apimatic"
CLI = WORKFLOW / "node_modules/@apimatic/cli/bin/run.js"
SDK = WORKFLOW / "generated/python"
CLI_VERSION = "2.0.0"


class WorkflowError(RuntimeError):
    """A constant diagnostic, never raw subprocess output or exception text."""


class Blocked(WorkflowError):
    """Required credentials are absent. No remote command has been invoked."""


def require_node(node: str = "node") -> None:
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowError("Node 24+ is required; node could not be started") from None
    version = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", result.stdout)
    if result.returncode or version is None or int(version[1]) < 24:
        raise WorkflowError("Node 24+ is required; Node 22 cannot run APIMatic CLI 2.0.0")


def check_spec() -> str:
    expected = render()
    if not DEFAULT_PATH.exists() or DEFAULT_PATH.read_text(encoding="utf-8") != expected:
        raise WorkflowError("OpenAPI drift: run python -m agentpal.platform.openapi --write")
    return hashlib.sha256(expected.encode("utf-8")).hexdigest()


def artifact_hashes(directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise WorkflowError("Generated artifacts must not contain symlinks")
        if path.is_file() and path.name != "agentpal-manifest.json":
            hashes[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return hashes


def verify_artifact(directory: Path = SDK) -> dict:
    try:
        manifest = json.loads((directory / "agentpal-manifest.json").read_text())
    except (OSError, ValueError):
        raise Blocked("BLOCKED: no APIMatic-generated SDK manifest; run generate with a key") from None
    if (manifest.get("spec_sha256") != check_spec()
            or manifest.get("cli_version") != CLI_VERSION
            or manifest.get("language") != "python"
            or not manifest.get("files")
            or manifest["files"] != artifact_hashes(directory)):
        raise WorkflowError("Generated SDK is stale or modified; regenerate before smoke testing")
    return manifest


def _run_cli(arguments: list[str], key: str, node: str, *, timeout: int = 600) -> None:
    require_node(node)
    try:
        package = json.loads((CLI.parents[1] / "package.json").read_text())
    except (OSError, ValueError):
        raise WorkflowError("CLI missing; run npm ci --prefix apimatic --cache /tmp/agentpal-npm") from None
    if package.get("version") != CLI_VERSION:
        raise WorkflowError("APIMatic CLI version mismatch; npm ci is required")
    # No shell, inherited login, persisted auth login or raw output on failures.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIMATIC_", "PAYPAL_", "AGENTPAL_"))}
    env["npm_config_cache"] = "/tmp/agentpal-npm"
    env["DO_NOT_TRACK"] = "1"
    try:
        result = subprocess.run([node, str(CLI), *arguments, "--auth-key", key],
                                cwd=ROOT, env=env, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise WorkflowError("APIMatic command timed out; no artifact was accepted") from None
    except OSError:
        raise WorkflowError("APIMatic command could not be started") from None
    if result.returncode:
        raise WorkflowError("APIMatic command failed; verify key, account entitlement and specification")
    # Keep diagnostics useful, but never expose the subprocess's raw output or key.
    # APIMatic's validation summary has no user credentials; explicit replacement is
    # still necessary because the CLI can echo arguments on parsing failures.
    summary = result.stdout.replace(key, "[REDACTED]")
    summary = re.sub(r"(?:apk|apo)_[A-Za-z0-9_-]+", "[REDACTED]", summary)
    reports = WORKFLOW / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / (arguments[0] + "-" + arguments[1] + ".txt")).write_text(summary)


def remote(action: str, *, node: str = "node") -> Path | None:
    key = os.environ.get("APIMATIC_API_KEY", "").strip()
    if not key:
        raise Blocked("BLOCKED BY CREDENTIALS: APIMATIC_API_KEY is absent; no remote call made")
    if action not in {"validate", "generate"}:
        raise WorkflowError("Unknown APIMatic workflow action")
    spec_hash = check_spec()
    with tempfile.TemporaryDirectory(prefix="agentpal-apimatic-") as temporary:
        stage = Path(temporary)
        spec_dir = stage / "src/spec"
        spec_dir.mkdir(parents=True)
        spec = spec_dir / "openapi.json"
        shutil.copyfile(DEFAULT_PATH, spec)
        shutil.copyfile(WORKFLOW / "src/apimatic.json", stage / "src/apimatic.json")
        _run_cli(["api", "validate", "--file", str(spec)], key, node)
        if action == "validate":
            return None
        output = stage / "sdk"
        _run_cli(["sdk", "generate", "--language", "python", "--input", str(stage),
                  "--destination", str(output)], key, node)
        files = artifact_hashes(output)
        if not files or not any(name.endswith("pyproject.toml") for name in files):
            raise WorkflowError("APIMatic returned no installable Python SDK")
        manifest = {"cli_version": CLI_VERSION, "language": "python",
                    "spec_sha256": spec_hash, "files": files}
        (output / "agentpal-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        SDK.parent.mkdir(parents=True, exist_ok=True)
        # Only replace this workflow's ignored output, after successful validation/generation.
        if SDK.exists():
            shutil.rmtree(SDK)
        shutil.copytree(output, SDK)
        return SDK
