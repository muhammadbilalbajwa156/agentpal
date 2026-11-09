"""The real v1 collection runs on the real CLI server and scrubs issued credentials."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_newman_live_v1_security(tmp_path):
    if not shutil.which("node") or not shutil.which("npx"):
        pytest.skip("live Newman requires node and npx")
    result = subprocess.run([sys.executable, "-m", "postman.run_newman", "--mode", "v1",
        "--reports-dir", str(tmp_path)], cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "V1 ready: real owner identity" in result.stdout
    assert "Local server stopped." in result.stdout
    report = json.loads((tmp_path / "v1.json").read_text())
    assert report["run"]["failures"] == []
    assert report["run"]["stats"]["requests"]["total"] == 17
    assert report["run"]["stats"]["assertions"]["total"] == 34
    assert report["run"]["stats"]["assertions"]["failed"] == 0
    serialized = json.dumps(report)
    assert not any(prefix in serialized for prefix in ("apk_", "apo_", "apt_"))
    assert "[REDACTED]" in serialized
    assert (tmp_path / "v1.xml").read_text().startswith("<?xml")
