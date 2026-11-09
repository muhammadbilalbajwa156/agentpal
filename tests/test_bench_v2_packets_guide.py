"""The annotator guide ships with every packet, uses only owner-policy clause ids, leaks
no AgentPal internals, and no bench v2 file points to a document that does not exist."""

from __future__ import annotations

import importlib.util
import json
import re
import sys

from agentpal.config import PACKAGE_ROOT

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"
GUIDE = V2 / "annotation" / "ANNOTATOR_GUIDE.md"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


packets = _load("bench_v2_make_packets_guide", V2 / "annotation" / "make_packets.py")


def test_every_packet_carries_the_guide_and_worked_examples(tmp_path):
    packets.make_packets(V2, ["a1", "a2", "a3"], tmp_path, PACKAGE_ROOT)
    for ann in ("a1", "a2", "a3"):
        d = tmp_path / ann
        assert (d / "ANNOTATOR_GUIDE.md").read_bytes() == GUIDE.read_bytes()
        assert (d / "worked_examples.jsonl").exists()
        ctx = json.loads((d / "context.json").read_text("utf-8"))
        assert "ANNOTATOR_GUIDE.md" in ctx["instructions"]


def test_guide_uses_only_real_policy_clauses_and_no_system_internals():
    text = GUIDE.read_text("utf-8")
    clauses = json.loads((V2 / "annotation" / "policy_clauses.json").read_text("utf-8"))
    used = set(re.findall(r"\bP-[A-Z]+(?:-[A-Z]+)*\b", text))
    assert used and used <= set(clauses) | {"P-BLOCK", "P-REVIEW"}
    for internal in ("LOOKALIKE_DOMAIN", "MERCHANT_DOMAIN_CHANGED", "orchestrator", "heuristic",
                     "risk_score", "rules.py", "features.py", "MODEL_", "PROMPT_INJECTION"):
        assert internal not in text, internal


def test_bench_v2_files_reference_only_existing_documents():
    for path in list(V2.rglob("*.py")) + list(V2.rglob("*.md")):
        for ref in re.findall(r"(?:docs|benchmarks)/[A-Za-z0-9_./-]+\.md", path.read_text("utf-8")):
            assert (PACKAGE_ROOT / ref).exists(), f"{path.name} references missing {ref}"
