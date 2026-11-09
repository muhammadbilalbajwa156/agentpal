"""Risk-model milestone: the training-data audit, the SFT dataset builder and the training
dry-run. These check properties that must hold before any GPU time is spent."""

from __future__ import annotations

import json

import pytest

from agentpal.dataset import write_dataset
from agentpal.schemas import ModelAnalysis
from training import audit, sft_data, train_lora


@pytest.fixture(scope="module")
def sft(tmp_path_factory):
    out = tmp_path_factory.mktemp("sft")
    return out, sft_data.build(out)


def rows(out, split):
    return [json.loads(line) for line in (out / f"{split}.jsonl").read_text().splitlines()]


def test_audit_reports_template_reuse_and_spurious_evidence(tmp_path):
    write_dataset(tmp_path / "d", seed=20260)
    r = audit.audit(tmp_path / "d", ml_model_path=None)
    d = r["diversity"]["all"]
    assert d["rows"] == 5 * d["scenario_groups"]          # 5 near-identical variations
    assert d["distinct_injection_sentences"] <= 8
    ov = r["cross_split_overlap"]["validation"]
    assert ov["template_seen_in_train"] / ov["rows"] > 0.5  # most validation is memorizable
    sp = r["spurious_correlations_train"]
    assert sp["evidence_absent"]["risky_rate"] - sp["evidence_present"]["risky_rate"] > 0.2
    gap = r["memorization_gap_validation"]
    assert gap["template_unseen"]["rows"] > 0 and gap["template_seen_in_train"]["rows"] > 0


def test_sft_splits_share_no_template_family_and_no_group(sft):
    out, manifest = sft
    tr, dev = rows(out, "train"), rows(out, "dev")
    assert tr and dev
    purpose = lambda r: json.loads(  # noqa: E731
        r["messages"][1]["content"].split("<untrusted_request>")[1]
        .split("</untrusted_request>")[0])["purpose"]
    assert not {purpose(r) for r in tr} & {purpose(r) for r in dev}
    assert not {r["group_id"] for r in tr} & {r["group_id"] for r in dev}
    blob = lambda rs: "\n".join(r["messages"][1]["content"] for r in rs)  # noqa: E731
    for s in audit.INJECTION_TEXTS:
        assert not (s in blob(tr) and s in blob(dev))


def test_sft_never_reads_retired_split_and_has_no_locked_test(sft):
    out, manifest = sft
    assert manifest["retired_test_split_read"] is False
    assert manifest["seed"] != 20260
    assert "external" in manifest["locked_test"]
    assert sorted(p.name for p in out.iterdir()) == ["dev.jsonl", "manifest.json", "train.jsonl"]


def test_sft_targets_are_schema_valid_and_never_block(sft):
    out, _ = sft
    for split in ("train", "dev"):
        for r in rows(out, split):
            t = ModelAnalysis.model_validate_json(r["target"])
            assert t.recommendation != "block"
            assert (t.recommendation == "require_review") == (r["label"] == 1)
            assert "deterministic_rule_findings" not in r["messages"][1]["content"]
            assert "agent_claimed_monthly_budget" in r["messages"][1]["content"]


def test_sft_decorrelates_claimed_evidence_from_label(sft):
    _, m = sft
    before, after = (m["spurious_correlation_train_before_debias"],
                     m["spurious_correlation_train_after_debias"])
    gap = lambda s: abs(s["evidence_absent"]["risky_rate"]  # noqa: E731
                        - s["evidence_present"]["risky_rate"])
    assert gap(before) > 0.2 and gap(after) < 0.1


def test_sft_build_is_deterministic(sft, tmp_path):
    _, first = sft
    again = sft_data.build(tmp_path / "again")
    assert {s: f["sha256"] for s, f in again["files"].items()} == \
        {s: f["sha256"] for s, f in first["files"].items()}


def test_training_dry_run_validates_dataset(sft):
    out, _ = sft
    stats = train_lora.validate(out, train_lora.TrainConfig())
    assert stats["train"]["rows"] > 0 and stats["dev"]["rows"] > 0
    assert train_lora.TrainConfig().base_model == "Qwen/Qwen3-1.7B"


@pytest.mark.parametrize("corrupt", ["block_target", "rule_findings", "manifest_flag"])
def test_training_dry_run_rejects_unsafe_data(sft, tmp_path, corrupt):
    out, _ = sft
    for name in ("train.jsonl", "dev.jsonl", "manifest.json"):
        (tmp_path / name).write_text((out / name).read_text())
    if corrupt == "manifest_flag":
        m = json.loads((tmp_path / "manifest.json").read_text())
        m["retired_test_split_read"] = True
        (tmp_path / "manifest.json").write_text(json.dumps(m))
    else:
        r = rows(out, "train")
        if corrupt == "block_target":
            t = json.loads(r[0]["target"])
            t["recommendation"] = "block"
            r[0]["target"] = json.dumps(t)
        else:
            r[0]["messages"][1]["content"] += '"deterministic_rule_findings": []'
        (tmp_path / "train.jsonl").write_text("".join(json.dumps(x) + "\n" for x in r))
    with pytest.raises(ValueError):
        train_lora.validate(tmp_path, train_lora.TrainConfig())
