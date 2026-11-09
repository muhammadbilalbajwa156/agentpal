"""training/intel_data.py: a separate, synthetic generator for the intelligence schema.

Deterministic, labelled as synthetic/AI-written, gated against the benchmarks with the
existing leakage tooling, and isolated from the pinned SFT build."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest

from agentpal.contracts import SourceEvidence
from agentpal.intelligence.research import assess_sources
from agentpal.schemas import PaymentRequest
from training import intel_data as gen

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def examples():
    return gen.build()


def test_build_is_deterministic(examples):
    assert json.dumps(gen.build(), sort_keys=True) == json.dumps(examples, sort_keys=True)
    assert json.dumps(gen.build(seed=1), sort_keys=True) != json.dumps(examples, sort_keys=True)


def test_every_example_is_labelled_synthetic_and_valid(examples):
    ids = [e["example_id"] for e in examples]
    assert len(ids) == len(set(ids))
    assert len({e["request"]["request_id"] for e in examples}) == len(examples)
    for e in examples:
        assert e["schema_version"] == gen.SCHEMA_VERSION
        assert e["data_origin"] == "synthetic_ai_written"
        assert "not real-world evidence" in e["provenance_note"]
        PaymentRequest.model_validate(e["request"])
        gen.IntelTarget.model_validate(e["target"])
        if e["page"]:
            from urllib.parse import urlsplit
            for url in (e["page"]["url"], e["page"]["final_url"]):
                if url:
                    assert urlsplit(url).hostname.endswith(".example")
        if e["request"]["merchant_domain"]:
            assert e["request"]["merchant_domain"].endswith(".example")
        if e["communication"]:
            assert e["communication"]["provenance"] == "claimed"
            assert e["communication"]["sender_verified"] is False


def test_model_targets_can_never_block_or_approve(examples):
    assert set(get_args(gen.ModelAdvisory)) == {"no_concern_found", "verify_before_proceeding",
                                                "recommend_review"}
    with pytest.raises(ValueError):
        gen.IntelModelOutput.model_validate({"advisory": "blocked_by_rules"})
    with pytest.raises(ValueError):
        gen.IntelModelOutput.model_validate({"advisory": "approve"})
    for e in examples:
        t = e["target"]
        if t["label"] == "risky":
            assert t["advisory"] == "recommend_review"
        if t["advisory"] == "no_concern_found":
            assert e["page"] and not t["discrepancy_codes"] and t["label"] == "legitimate"


def test_all_categories_and_both_labels_where_needed(examples):
    cats = Counter(e["category"] for e in examples)
    assert set(cats) == set(gen.CATEGORIES)
    for cat in ("multilingual_instructions", "communication_manipulation"):
        labels = {e["target"]["label"] for e in examples if e["category"] == cat}
        assert labels == {"risky", "legitimate"}  # language/channel alone is not the signal
    langs = {e["communication"]["language"] for e in examples
             if e["category"] == "multilingual_instructions"}
    assert len(langs) >= 8


def test_page_labels_are_reproducible_from_the_deterministic_code(examples):
    for e in examples:
        if not e["page"]:
            assert e["target"]["discrepancy_codes"] == []
            continue
        body = e["page"]["html"].encode()
        src = SourceEvidence(url=e["page"]["url"], final_url=e["page"]["final_url"],
                             retrieved_at=gen.RETRIEVED_AT, method="fixture",
                             content_sha256=SourceEvidence.digest(body))
        _, ds = assess_sources(PaymentRequest.model_validate(e["request"]), [(src, body)])
        assert sorted({d.code for d in ds}) == e["target"]["discrepancy_codes"]


def test_expected_discrepancies_per_category(examples):
    def codes(cat):
        return {tuple(e["target"]["discrepancy_codes"]) for e in examples if e["category"] == cat}
    assert codes("genuine") == {()}
    assert codes("unusual_but_legit") == {()}
    assert codes("price_mismatch") == {("PRICE_MISMATCH",)}
    assert all("MERCHANT_DOMAIN_MISMATCH" in c for c in codes("merchant_impersonation"))
    assert codes("misleading_evidence") == {("MERCHANT_DOMAIN_MISMATCH",),
                                            ("CURRENCY_MISMATCH",), ("OUT_OF_STOCK",)}


def test_splits_are_family_disjoint(examples):
    fams = {s: {e["family"] for e in examples if e["split"] == s} for s in ("train", "dev")}
    assert fams["train"] and fams["dev"] and not fams["train"] & fams["dev"]


def test_leakage_gate_passes(examples):
    rep = gen.leakage_report(examples)
    assert rep["passed"], rep["findings_by_type"]


def test_leakage_gate_catches_a_planted_benchmark_message(examples):
    bench = [json.loads(line) for line in
             (ROOT / "benchmarks" / "agentpal-bench-v2" / "cases.jsonl").read_text().splitlines()
             if line.strip()]
    text = next(c["request"]["purpose"] for c in bench
                if len(c["request"].get("purpose", "").split()) >= 6)
    planted = json.loads(json.dumps(examples[0]))
    planted["communication"] = {"channel": "email", "language": "en", "text": text,
                                "provenance": "claimed", "sender_verified": False}
    assert not gen.leakage_report([planted])["passed"]


def test_write_produces_files_and_manifest(tmp_path):
    manifest = gen.write(tmp_path / "intel")
    files = sorted(p.name for p in (tmp_path / "intel").iterdir())
    assert files == ["intel_dev.jsonl", "intel_manifest.json", "intel_train.jsonl"]
    assert manifest["data_origin"] == "synthetic_ai_written"
    assert manifest["leakage"]["passed"] is True
    assert "No model has been trained" in manifest["not_used_for_training"]
    assert sum(f["rows"] for f in manifest["files"].values()) == sum(
        manifest["categories"].values())


def test_evaluate_scores_risky_missed_and_invalid(examples):
    perfect = {e["example_id"]: {k: v for k, v in e["target"].items() if k != "label"}
               for e in examples}
    rep = gen.evaluate(examples, perfect)
    assert all(c["risky_missed"] == 0 and c["legitimate_flagged"] == 0 and c["invalid"] == 0
               for c in rep["per_category"].values())
    assert rep["discrepancy_codes"]["fp"] == 0 and rep["discrepancy_codes"]["fn"] == 0
    lazy = {e["example_id"]: {"advisory": "no_concern_found"} for e in examples}
    rep = gen.evaluate(examples, lazy)
    assert sum(c["risky_missed"] for c in rep["per_category"].values()) == sum(
        e["target"]["label"] == "risky" for e in examples)
    blocked = {e["example_id"]: {"advisory": "blocked_by_rules"} for e in examples}
    rep = gen.evaluate(examples, blocked)
    assert sum(c["invalid"] for c in rep["per_category"].values()) == len(examples)


def test_generator_is_isolated_from_the_pinned_sft_build():
    tree = ast.parse((ROOT / "training" / "intel_data.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = {"training.sft_data", "training.train_lora", "training.modal_train",
                 "training.export", "training.evaluate_model"}
    assert not {i for i in imported if any(i.startswith(f) for f in forbidden)}
    # it only reads the overlap guard from augment, and never writes outside --out
    assert "training.augment.overlap_report" in imported
