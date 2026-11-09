"""P0.7d regression: model artifacts are verified against a trusted SHA-256 digest before
loading. A matching digest establishes only that the file equals the pinned file; it says
nothing about the model's safety or provenance.

Before the fix, the GGUF and JSON artifacts were loaded without any integrity check.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentpal import integrity
from agentpal.__main__ import main as cli
from agentpal.analyzers import LlamaCppAnalyzer, MLAnalyzer, build_analyzer_chain
from agentpal.analyzers import llm as llm_module
from agentpal.config import PACKAGE_ROOT, Settings
from agentpal.orchestrator import assess

from conftest import ctx, make_request

REAL_ML = PACKAGE_ROOT / "artifacts" / "baseline_classifier.json"


@pytest.fixture
def ml_artifact(tmp_path) -> Path:
    if not REAL_ML.exists():
        pytest.skip("trained ML artifact not present (run `python -m agentpal train`)")
    p = tmp_path / "model.json"
    p.write_bytes(REAL_ML.read_bytes())
    return p


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_ml_verified_with_trusted_digest_is_used(policy, ml_artifact):
    a = MLAnalyzer(ml_artifact, expected_sha256=sha(ml_artifact), require_integrity=True)
    assert a.available()
    out = assess(make_request(), ctx(), policy, [a])
    assert out.model.backend == "ml"


def test_corrupted_ml_artifact_is_refused_and_falls_back(policy, ml_artifact):
    digest = sha(ml_artifact)
    data = bytearray(ml_artifact.read_bytes())
    data[-5] ^= 0x01  # flip one bit
    ml_artifact.write_bytes(bytes(data))
    a = MLAnalyzer(ml_artifact, expected_sha256=digest, require_integrity=True)
    assert not a.available()
    assert "integrity" in a.unavailable_reason
    out = assess(make_request(), ctx(), policy, [a])
    assert out.model.backend == "heuristic"


def test_missing_required_digest_makes_backend_unavailable(ml_artifact):
    a = MLAnalyzer(ml_artifact, expected_sha256=None, require_integrity=True)
    assert not a.available() and "no trusted digest" in a.unavailable_reason


def test_explicit_opt_out_loads_without_digest(ml_artifact):
    assert MLAnalyzer(ml_artifact, expected_sha256=None, require_integrity=False).available()


def test_digest_comparison_is_exact_and_case_insensitive(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    digest = sha(p)
    assert integrity.verify_file(p, digest.upper()) == b"abc"
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_file(p, digest[:-1] + ("0" if digest[-1] != "0" else "1"))
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_file(p, "not-a-digest")


class ExplodingLlama:
    def __init__(self, *a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("Llama() constructed for an unverified file")


def test_corrupted_gguf_is_never_loaded(tmp_path, monkeypatch):
    llama_cpp = pytest.importorskip("llama_cpp")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF" + b"\x00" * 1024)
    pinned = sha(model)
    model.write_bytes(b"GGUF" + b"\x01" * 1024)  # replaced after pinning
    monkeypatch.setattr(llama_cpp, "Llama", ExplodingLlama)
    a = LlamaCppAnalyzer(model, expected_sha256=pinned, require_integrity=True)
    assert not a.available()
    assert "integrity" in a.unavailable_reason


def test_verified_gguf_is_loaded(tmp_path, monkeypatch):
    llama_cpp = pytest.importorskip("llama_cpp")
    model = tmp_path / "ok.gguf"
    model.write_bytes(b"GGUF" + b"\x02" * 1024)
    constructed = []

    class FakeLlama:
        def __init__(self, model_path, **k):
            constructed.append(model_path)

    monkeypatch.setattr(llama_cpp, "Llama", FakeLlama)
    monkeypatch.delitem(llm_module._LLAMA_CACHE, str(model), raising=False)
    # digest verification only; read-only storage + staging are tested in test_od_decision_7
    a = LlamaCppAnalyzer(model, expected_sha256=sha(model), require_integrity=True,
                         require_read_only_storage=False)
    assert a.available()
    a._llm()
    assert constructed == [str(model)]


def test_manifest_wires_digests_into_the_chain(tmp_path, ml_artifact):
    manifest = tmp_path / "model_manifest.json"
    manifest.write_text(json.dumps({"manifest_version": 1, "models": {
        "ml": {"sha256": sha(ml_artifact), "source": "test"}}}))
    s = Settings(analyzer_chain=["ml", "heuristic"], ml_model_path=str(ml_artifact),
                 model_manifest_path=str(manifest), require_model_integrity=True)
    chain = build_analyzer_chain(s)
    assert chain[0].name == "ml" and chain[0].available()
    # same chain, manifest without an ML entry -> unavailable, rules still work
    manifest.write_text(json.dumps({"manifest_version": 1, "models": {}}))
    assert not build_analyzer_chain(s)[0].available()


def test_missing_manifest_file_fails_closed(tmp_path, ml_artifact):
    s = Settings(analyzer_chain=["ml", "heuristic"], ml_model_path=str(ml_artifact),
                 model_manifest_path=str(tmp_path / "absent.json"), require_model_integrity=True)
    assert not build_analyzer_chain(s)[0].available()


def test_pin_model_cli_records_digest_in_manifest(tmp_path, ml_artifact, capsys):
    manifest = tmp_path / "manifest.json"
    cli(["pin-model", "ml", str(ml_artifact), "--manifest", str(manifest), "--source", "unit"])
    data = json.loads(manifest.read_text())
    assert data["models"]["ml"]["sha256"] == sha(ml_artifact)
    assert data["models"]["ml"]["source"] == "unit"
    assert sha(ml_artifact) in capsys.readouterr().out


def test_repository_manifest_pins_the_trained_model_with_provenance():
    """The llama.cpp pin is the fine-tuned AgentPal model (it replaced the zero-shot
    Qwen2.5 build, one-model strategy). The pinned digest must be the GGUF hash in the
    committed export manifest of a real training run of the pinned base revision."""
    m = integrity.load_manifest(PACKAGE_ROOT / "config" / "model_manifest.json")
    export = json.loads((PACKAGE_ROOT / "docs" / "training_runs" /
                         "2026-10-30-modal-run2" / "EXPORT_MANIFEST.json").read_text())
    gguf = export["files"]["agentpal-qwen3-1.7b-q4_k_m.gguf"]["sha256"]
    assert m.digest_for("llamacpp") == gguf
    assert export["run_kind"] == "training"
    assert export["base_model"] == "Qwen/Qwen3-1.7B"
    assert export["base_revision"] == "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
