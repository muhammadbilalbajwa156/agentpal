"""The Kaggle source bundle (private repo, no credentials in the handoff): it contains only
tracked agentpal/ files of one commit, and `verify` rejects any changed file."""

from __future__ import annotations

import shutil
import subprocess
import zipfile

import pytest

from training import kaggle_bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    if shutil.which("git") is None or subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True).returncode != 0:
        pytest.skip("needs a git checkout")
    out = tmp_path_factory.mktemp("bundle")
    zip_path = kaggle_bundle.create(out)
    root = out / "x"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(root)
    return zip_path, root / "agentpal"


def test_bundle_holds_only_tracked_source(bundle):
    zip_path, _ = bundle
    names = zipfile.ZipFile(zip_path).namelist()
    assert all(n.startswith("agentpal/") for n in names)
    for forbidden in ("/data/train.jsonl", ".gguf", "/build/", "baseline_classifier.json"):
        assert not any(forbidden in n for n in names), forbidden
    assert not any(n.endswith("/.env") for n in names)
    assert (zip_path.parent / f"{zip_path.name}.sha256").exists()


def test_verify_works_with_a_relative_root(bundle, monkeypatch):
    """Found by the fresh-bundle Kaggle simulation: `verify --root .` failed every file."""
    _, root = bundle
    monkeypatch.chdir(root)
    kaggle_bundle.verify(kaggle_bundle.Path("."))


def test_verify_accepts_the_bundle_and_rejects_tampering(bundle):
    _, root = bundle
    info = kaggle_bundle.verify(root)
    assert len(info["commit"]) == 40
    target = root / "agentpal" / "rules.py"
    target.write_text(target.read_text() + "\n# tampered\n")
    with pytest.raises(SystemExit, match="FAILED"):
        kaggle_bundle.verify(root)
