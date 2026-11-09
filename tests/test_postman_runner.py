"""Exercise the real CLI server and Newman, including report credential hygiene."""

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from postman.run_newman import redact
from postman import run_newman


ROOT = Path(__file__).resolve().parents[1]


def test_redact_removes_every_runtime_credential():
    secrets = ["throwaway-" + "a" * 40, "throwaway-" + "b" * 40]
    raw = json.dumps({"authorization": "Bearer " + secrets[0], "variable": secrets[1]})
    cleaned = redact(raw, secrets)
    assert all(secret not in cleaned for secret in secrets)
    assert cleaned.count("[REDACTED]") == 2


def test_newman_live_legacy_security(tmp_path):
    if not shutil.which("node") or not shutil.which("npx"):
        pytest.skip("live Newman requires node and npx")
    result = subprocess.run(
        [sys.executable, "-m", "postman.run_newman", "--reports-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True, timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Local server stopped." in result.stdout
    report = json.loads((tmp_path / "legacy.json").read_text())
    assert report["run"]["failures"] == []
    assert report["run"]["stats"]["requests"]["total"] >= 8
    assert report["run"]["stats"]["assertions"]["total"] >= 20
    assert report["run"]["stats"]["assertions"]["failed"] == 0
    assert (tmp_path / "legacy.xml").read_text().startswith("<?xml")
    serialized = json.dumps(report)
    assert "apt_" not in serialized
    assert "[REDACTED]" in serialized
    # The runner's finally block must stop the actual network listener.
    raw_url = report["run"]["executions"][0]["request"]["url"]
    port = int(raw_url["port"]) if isinstance(raw_url, dict) else urlsplit(raw_url).port
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_pending_v1_skips_without_starting_server(tmp_path):
    if (ROOT / "docs/api/openapi.json").exists():
        pytest.skip("local v1 specification is now available")
    result = subprocess.run(
        [sys.executable, "-m", "postman.run_newman", "--mode", "v1",
         "--reports-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert "PENDING v1" in result.stdout
    assert not list(tmp_path.iterdir())


def test_newman_timeout_still_stops_real_server(monkeypatch, tmp_path):
    processes = []
    original_popen = subprocess.Popen

    def start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def run(command, **kwargs):
        if command[0] == "npm":
            return subprocess.CompletedProcess(command, 0)
        raise subprocess.TimeoutExpired(command, 90)

    monkeypatch.setattr(run_newman.subprocess, "Popen", start)
    monkeypatch.setattr(run_newman.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_newman.run(reports_dir=tmp_path)
    assert len(processes) == 1
    assert processes[0].poll() is not None
