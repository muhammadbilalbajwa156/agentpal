"""Owner decision 7: integrity stays required; GGUF models must sit on read-only storage
and are loaded through a controlled, hash-while-copy staging step, so the file llama.cpp
loads is exactly the bytes that matched the pinned digest."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from agentpal import integrity
from agentpal.analyzers import LlamaCppAnalyzer, MLAnalyzer
from agentpal.analyzers import llm as llm_module
from agentpal.config import Settings

llama_cpp = pytest.importorskip("llama_cpp")

CONTENT = b"GGUF" + bytes(range(256)) * 64


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def model(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    f = d / "m.gguf"
    f.write_bytes(CONTENT)
    return f


def make_read_only(f: Path) -> None:
    f.chmod(0o444)
    f.parent.chmod(0o555)


@pytest.fixture(autouse=True)
def _restore_permissions(tmp_path):
    yield
    for p in tmp_path.rglob("*"):
        try:
            p.chmod(0o700 if p.is_dir() else 0o600)
        except OSError:
            pass


@pytest.fixture
def fake_llama(monkeypatch):
    loaded = []

    class FakeLlama:
        def __init__(self, model_path, **k):
            loaded.append(Path(model_path))

    monkeypatch.setattr(llama_cpp, "Llama", FakeLlama)
    monkeypatch.setattr(llm_module, "_LLAMA_CACHE", {})
    monkeypatch.setattr(llm_module, "_VERIFIED", set())
    return loaded


def analyzer(f, tmp_path, **kw):
    return LlamaCppAnalyzer(f, expected_sha256=sha(CONTENT), require_integrity=True,
                            staging_dir=tmp_path / "staging", **kw)


def test_integrity_required_by_default():
    assert Settings().require_model_integrity is True
    assert Settings().require_read_only_model_storage is True


def test_writable_model_storage_is_refused(model, tmp_path, fake_llama):
    a = analyzer(model, tmp_path)
    assert not a.available() and "read-only" in a.unavailable_reason
    assert fake_llama == []


def test_read_only_storage_loads_a_verified_private_copy(model, tmp_path, fake_llama):
    make_read_only(model)
    a = analyzer(model, tmp_path)
    assert a.available()
    a._llm()
    assert len(fake_llama) == 1
    staged = fake_llama[0]
    assert staged.parent == tmp_path / "staging" and staged != model
    assert sha(staged.read_bytes()) == sha(CONTENT)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o400
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700


def test_source_change_after_staging_cannot_affect_loaded_bytes(model, tmp_path, fake_llama):
    make_read_only(model)
    a = analyzer(model, tmp_path)
    a._llm()
    model.parent.chmod(0o755)
    model.chmod(0o644)
    model.write_bytes(b"GGUF-evil")           # attacker rewrites the source afterwards
    assert sha(fake_llama[0].read_bytes()) == sha(CONTENT)


def test_copy_that_does_not_match_is_never_loaded(model, tmp_path, fake_llama, monkeypatch):
    make_read_only(model)
    a = analyzer(model, tmp_path)
    a.available()  # verified (and cached) before the swap
    # simulate the source changing between verification and staging
    real_open = open

    def tampered_open(path, mode="r", *args, **kw):
        if Path(path) == model and "rb" in mode:
            import io
            return io.BytesIO(b"GGUF-swapped")
        return real_open(path, mode, *args, **kw)

    monkeypatch.setattr("builtins.open", tampered_open)
    with pytest.raises(Exception, match="integrity"):
        a._llm()
    assert fake_llama == []
    assert not any((tmp_path / "staging").glob("*.gguf"))


def test_insecure_staging_directory_is_refused(model, tmp_path, fake_llama):
    make_read_only(model)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o777)
    staging.chmod(0o777)
    a = analyzer(model, tmp_path)
    with pytest.raises(Exception, match="staging"):
        a._llm()
    assert fake_llama == []


def test_read_only_check_helper():
    assert integrity.storage_is_read_only  # documented helper exists


def test_ml_artifact_is_verified_from_memory_without_staging(tmp_path):
    """The ML JSON is parsed from the exact verified bytes (no path-based load), so the
    read-only requirement does not apply to it; integrity is still required. Uses its own
    file, so it does not depend on the git-ignored trained artifact (fresh-clone CI)."""
    artifact = tmp_path / "baseline_classifier.json"
    artifact.write_text("{}")  # present but unpinned: refused before it is parsed
    m = MLAnalyzer(artifact, expected_sha256=None, require_integrity=True)
    assert not m.available() and "no trusted digest" in m.unavailable_reason


def test_service_warm_up_verifies_and_stages_before_serving(model, tmp_path, fake_llama):
    from agentpal.analyzers import HeuristicAnalyzer
    from agentpal.config import load_policy
    from agentpal.service import RiskService
    from agentpal.store import AssessmentStore

    make_read_only(model)
    s = RiskService(load_policy(), AssessmentStore(":memory:"),
                    [analyzer(model, tmp_path), HeuristicAnalyzer()])
    assert s.warm_up() == {"llamacpp": True, "heuristic": True}
    assert len(fake_llama) == 1  # loaded once, from the staged copy, before any request


def test_llama_uses_all_cpu_cores_by_default(model, tmp_path, monkeypatch):
    """llama-cpp-python's default uses half the CPUs for generation; the analyzer passes
    every core for generation and prompt processing (latency margin to the timeout)."""
    seen = {}

    class FakeLlama:
        def __init__(self, model_path, **k):
            seen.update(k)

    monkeypatch.setattr(llama_cpp, "Llama", FakeLlama)
    monkeypatch.setattr(llm_module, "_LLAMA_CACHE", {})
    monkeypatch.setattr(llm_module, "_VERIFIED", set())
    monkeypatch.setattr(llm_module.os, "cpu_count", lambda: 6)
    make_read_only(model)
    analyzer(model, tmp_path, disable_thinking=False)._llm()
    assert seen["n_threads"] == 6 and seen["n_threads_batch"] == 6
