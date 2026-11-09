"""Offline tests of real invocation boundaries, never claims of APIMatic service success."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from apimatic import workflow as w
from apimatic.__main__ import main


@pytest.mark.parametrize("action", ["validate", "generate"])
def test_absent_key_blocks_before_any_subprocess(monkeypatch, action):
    monkeypatch.delenv("APIMATIC_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("missing key must never invoke a subprocess or remote service")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(w.Blocked, match="BLOCKED BY CREDENTIALS"):
        w.remote(action)
    assert main([action]) == 2


@pytest.mark.parametrize("version", ["v22.22.0", "v20.0.0", "unexpected"])
def test_unsupported_node_fails_clearly(monkeypatch, version):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout=version, returncode=0))
    with pytest.raises(w.WorkflowError, match="Node 24\\+"):
        w.require_node()


def test_missing_node_fails_clearly(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(w.WorkflowError, match="Node 24\\+"):
        w.require_node()


def cli_fixture(monkeypatch, tmp_path):
    config = (w.WORKFLOW / "src/apimatic.json").read_text()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/apimatic.json").write_text(config)
    cli = tmp_path / "cli/bin/run.js"
    cli.parent.mkdir(parents=True)
    (cli.parents[1] / "package.json").write_text('{"version":"2.0.0"}')
    monkeypatch.setattr(w, "CLI", cli)
    monkeypatch.setattr(w, "WORKFLOW", tmp_path)
    monkeypatch.setattr(w, "require_node", lambda node: None)


def test_key_is_environment_only_and_never_persisted(monkeypatch, tmp_path):
    cli_fixture(monkeypatch, tmp_path)
    secret = "test-only-apimatic-secret-not-a-real-key"
    monkeypatch.setenv("APIMATIC_API_KEY", secret)
    monkeypatch.setenv("PAYPAL_SANDBOX_CLIENT_SECRET", "do-not-inherit")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="diagnostic " + secret)

    monkeypatch.setattr(subprocess, "run", run)
    w.remote("validate")
    argv, kwargs = calls[0]
    assert argv[-2:] == ["--auth-key", secret]
    assert kwargs["env"]["npm_config_cache"].startswith("/tmp/")
    assert "APIMATIC_API_KEY" not in kwargs["env"]
    assert "PAYPAL_SANDBOX_CLIENT_SECRET" not in kwargs["env"]
    assert not kwargs.get("shell")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()


@pytest.mark.parametrize("failure", ["exit", "timeout", "start"])
def test_cli_failures_suppress_raw_credentials(monkeypatch, tmp_path, failure, capsys):
    cli_fixture(monkeypatch, tmp_path)
    secret = "test-only-do-not-print-this-value"

    def run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output=secret)
        if failure == "start":
            raise OSError(secret)
        return SimpleNamespace(returncode=1, stdout=secret, stderr=secret)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(w.WorkflowError) as exc:
        w._run_cli(["api", "validate"], secret, "node")
    assert secret not in str(exc.value)
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert not (tmp_path / "reports").exists()


def test_drift_refuses_remote_invocation(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMATIC_API_KEY", "test-only-key")
    spec = tmp_path / "spec.json"
    spec.write_text('{}')
    monkeypatch.setattr(w, "DEFAULT_PATH", spec)
    monkeypatch.setattr(w, "_run_cli", lambda *a, **k: pytest.fail("drift reached CLI"))
    with pytest.raises(w.WorkflowError, match="drift"):
        w.remote("validate")


def test_generation_manifest_fences_changed_spec_and_output(monkeypatch, tmp_path):
    """A transport fixture tests bookkeeping; it is explicitly NOT a generated SDK."""
    monkeypatch.setenv("APIMATIC_API_KEY", "test-only-key")
    monkeypatch.setattr(w, "SDK", tmp_path / "generated/python")

    def fixture_cli(arguments, *args, **kwargs):
        if arguments[:2] == ["sdk", "generate"]:
            output = Path(arguments[arguments.index("--destination") + 1])
            output.mkdir()
            (output / "pyproject.toml").write_text('# bookkeeping fixture, not an SDK\n')

    monkeypatch.setattr(w, "_run_cli", fixture_cli)
    directory = w.remote("generate")
    manifest = w.verify_artifact(directory)
    assert manifest["cli_version"] == "2.0.0"
    assert len(manifest["spec_sha256"]) == 64
    (directory / "pyproject.toml").write_text('# modified fixture\n')
    with pytest.raises(w.WorkflowError, match="stale or modified"):
        w.verify_artifact(directory)
    (directory / "pyproject.toml").write_text('# bookkeeping fixture, not an SDK\n')
    monkeypatch.setattr(w, "check_spec", lambda: "changed-spec")
    with pytest.raises(w.WorkflowError, match="stale or modified"):
        w.verify_artifact(directory)


def test_failed_generation_preserves_existing_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMATIC_API_KEY", "test-only-key")
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "sentinel").write_text("previous artifact")
    monkeypatch.setattr(w, "SDK", sdk)

    def reject(*args, **kwargs):
        raise w.WorkflowError("rejected")

    monkeypatch.setattr(w, "_run_cli", reject)
    with pytest.raises(w.WorkflowError):
        w.remote("generate")
    assert (sdk / "sentinel").read_text() == "previous artifact"


def test_missing_sdk_and_symlink_artifacts_fail_closed(tmp_path):
    with pytest.raises(w.Blocked, match="no APIMatic-generated SDK"):
        w.verify_artifact(tmp_path)
    (tmp_path / "outside").symlink_to(__file__)
    with pytest.raises(w.WorkflowError, match="symlinks"):
        w.artifact_hashes(tmp_path)


def test_pinned_cli_and_lockfile():
    package = json.loads((w.WORKFLOW / "package.json").read_text())
    lock = json.loads((w.WORKFLOW / "package-lock.json").read_text())
    assert package["devDependencies"]["@apimatic/cli"] == "2.0.0"
    assert lock["packages"]["node_modules/@apimatic/cli"]["version"] == "2.0.0"
    assert package["engines"]["node"] == ">=24.0.0"
