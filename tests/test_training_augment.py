"""SFT data quality (risk-model step 2): the hand-written augmentation and the rebuilt SFT
set. Labels must not be inferable from evidence presence, template wording, language or
evidence type; train/dev share no template family; nothing overlaps the benchmarks."""

from __future__ import annotations

import collections
import json
import random
from pathlib import Path

import pytest

from agentpal.schemas import ModelAnalysis
from training import augment, sft_data

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "benchmarks" / "agentpal-bench-v2"


@pytest.fixture(scope="module")
def aug():
    return augment.generate(7319)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("sft2")
    m = sft_data.build(out)
    recs = {s: [json.loads(x) for x in (out / f"{s}.jsonl").read_text().splitlines()]
            for s in ("train", "dev")}
    return out, m, recs


def _bench_purpose() -> str:
    return json.loads((V2 / "cases.jsonl").read_text().splitlines()[0])["request"]["purpose"]


# ------------------------------------------------------------------ overlap guard

def test_augmentation_and_full_sft_do_not_overlap_benchmarks(aug, built):
    assert augment.overlap_report(aug)["passed"]
    _, m, _ = built
    bo = m["benchmark_overlap"]
    assert bo["passed"] and bo["augmented_vs_bench_v1_passed"]
    assert "agentpal-bench-v2" in bo["reference_sources"]


@pytest.mark.parametrize("transform", ["verbatim", "suffix", "homoglyph", "zero_width",
                                       "leet"])
def test_overlap_check_fails_on_planted_benchmark_text(aug, transform):
    text = _bench_purpose()
    rng = random.Random(1)
    planted = {
        "verbatim": text,
        "suffix": text + " for the team",
        "homoglyph": augment.obfuscate(text, "homoglyph", rng),
        "zero_width": augment.obfuscate(text, "zero_width", rng),
        "leet": augment.obfuscate(text, "leet", rng),
    }[transform]
    assert planted != text or transform == "verbatim"
    row = json.loads(json.dumps(aug[0]))
    row["request"]["purpose"] = planted
    rep = augment.overlap_report(aug[:5] + [row])
    assert not rep["passed"]
    assert any(f["unit"] == row["example_id"] for f in rep["findings"])


def test_overlap_check_fails_on_benchmark_merchant_and_domain(aug):
    case = next(json.loads(x) for x in (V2 / "cases.jsonl").read_text().splitlines()
                if json.loads(x)["request"]["merchant_domain"] not in
                {d for _, d, _ in augment.APPROVED})
    row = json.loads(json.dumps(aug[0]))
    row["request"]["merchant_name"] = case["request"]["merchant_name"]
    row["request"]["merchant_domain"] = case["request"]["merchant_domain"]
    rep = augment.overlap_report([row])
    assert {"DOMAIN", "MERCHANT_NAME"} & set(rep["findings_by_type"])


def test_sft_build_refuses_to_write_overlapping_data(tmp_path, monkeypatch):
    real = augment.generate

    def leaky(seed):
        rows = real(seed)
        rows[0]["request"]["business_justification"] = _bench_purpose()
        return rows

    monkeypatch.setattr(augment, "generate", leaky)
    with pytest.raises(ValueError, match="overlaps"):
        sft_data.build(tmp_path / "x")
    assert not (tmp_path / "x" / "train.jsonl").exists()


# ------------------------------------------------------------------ splits

def test_families_never_cross_splits(aug, built):
    by = collections.defaultdict(set)
    for e in aug:
        by[e["family"]].add(e["split"])
    assert all(len(v) == 1 for v in by.values())
    pools = augment.family_pools()
    for e in aug:  # every element of an example comes from its split's pools
        assert pools.split.get(e["family"].split("|")[-1], e["split"]) == e["split"] or \
            e["family"].startswith("missing:")
    _, m, recs = built
    assert m["families_shared_by_train_and_dev"] == 0
    assert not ({r["family"] for r in recs["train"]} & {r["family"] for r in recs["dev"]})
    assert not ({r["group_id"] for r in recs["train"]} & {r["group_id"] for r in recs["dev"]})


def test_every_pool_has_train_and_dev_families():
    pools = augment.family_pools()
    for prefix in ("inj-en", "inj-ml", "legit-ml", "mismatch", "nearname", "dup", "renewal",
                   "invoice", "note", "urgent", "newmerchant"):
        assert pools.of(prefix, "train") and pools.of(prefix, "dev"), prefix


def test_translations_and_obfuscations_share_their_family(aug):
    for e in aug:
        if e["category"] == "aug_injection_obfuscated":
            i = int(e["family"].split(":")[1])
            blob = json.dumps(e["request"], ensure_ascii=False)
            assert augment.INJ_EN[i] not in blob            # really obfuscated
            assert e["family"].startswith("inj-en:")         # same family as the plain one
        if e["category"] == "aug_injection_multilingual":
            assert e["family"].startswith("inj-ml:") and e["language"] != "en"


@pytest.mark.parametrize("style", augment.OBFUSCATIONS)
def test_deobfuscate_recovers_the_sentence(style):
    for i, s in enumerate(augment.INJ_EN):
        o = augment.obfuscate(s, style, random.Random(i))
        assert o != s
        d = augment.deobfuscate(o)
        if style == "spaced":  # word boundaries between spaced words are ambiguous
            squash = lambda t: "".join(ch for ch in t.casefold() if ch.isalnum())  # noqa: E731
            assert squash(d) == squash(s)
        elif style != "leet":  # leet digits that are real digits stay ambiguous
            assert d.casefold() == s.casefold()


# ------------------------------------------------------------------ shortcuts

def _gap(stats: dict) -> float:
    return abs(stats["evidence_present"]["risky_rate"] - stats["evidence_absent"]["risky_rate"])


def test_evidence_presence_does_not_predict_the_label_in_train_or_dev(built):
    _, m, _ = built
    assert _gap(m["spurious_correlation_train_before_debias"]) > 0.2   # the generator's bias
    assert _gap(m["spurious_correlation_train_after_debias"]) < 0.05
    assert _gap(m["spurious_correlation_dev_after_debias"]) < 0.1


def test_note_evidence_and_non_english_text_appear_with_both_labels(built):
    _, _, recs = built
    for split in ("train", "dev"):
        rows = recs[split]
        note = [r for r in rows if '"type": "note"' in r["messages"][1]["content"]]
        assert {r["label"] for r in note} == {0, 1}, split
        ne = [r for r in rows if r["language"] != "en"]
        assert {r["label"] for r in ne} == {0, 1}, split
    ne = [r for r in recs["train"] if r["language"] != "en"]
    assert 0.25 < sum(r["label"] for r in ne) / len(ne) < 0.75


def test_fabricated_evidence_is_risky_only_with_another_visible_signal(aug):
    for e in aug:
        if e["category"] == "aug_fabricated_evidence_with_signal":
            assert e["label"] == 1 and e["signals"]
            assert all(code != "EVIDENCE" for code, _ in e["signals"])
        if e["category"] == "aug_evidence_routine_legit":
            assert e["label"] == 0 and e["request"]["evidence"] and not e["signals"]


def test_rows_whose_label_the_prompt_cannot_show_are_dropped(built):
    _, m, recs = built
    dropped = m["dropped_label_not_visible_in_model_input"]
    assert dropped["duplicate"] > 0 and dropped["unknown_reference"] > 0
    cats = {r["category"] for s in recs.values() for r in s}
    assert "duplicate" not in cats  # generator duplicates are invisible to the model
    assert "aug_duplicate_visible" in cats


def test_targets_are_varied_grounded_and_never_block(built):
    _, m, recs = built
    for split, rows in recs.items():
        assert m["files"][split]["distinct_targets"] > 0.8 * len(rows)
        for r in rows:
            t = ModelAnalysis.model_validate_json(r["target"])
            assert t.recommendation != "block"
            assert (t.recommendation == "require_review") == (r["label"] == 1)
            assert (t.risk_score >= 50) == (r["label"] == 1)
            if r["label"] == 1 and r["category"] != "suspicious_combination":
                assert t.findings and all(f.evidence for f in t.findings)
            for f in t.findings:  # evidence quotes the request; nothing is invented
                quoted = f.evidence.split("'")[1] if "'" in f.evidence else None
                if quoted and r["category"] in ("purpose_mismatch", "aug_purpose_mismatch"):
                    assert quoted in r["messages"][1]["content"]


def test_class_balance_and_categories_are_reported(built):
    _, m, _ = built
    for split in ("train", "dev"):
        f = m["files"][split]
        assert 0.4 < f["risky_share"] < 0.65
        assert sum(f["categories"].values()) == f["rows"]
        assert sum(f["languages"].values()) == f["rows"]
        assert len(f["categories"]) >= 15


def test_round2_families_have_visible_signals_and_correct_labels():
    """aug-2 targets the first trained model's dev errors: self-made evidence (risky),
    non-English purpose mismatch (risky), risky-sounding but legitimate topics."""
    from training import augment as a

    ex = a.generate(7319)
    by = {}
    for e in ex:
        by.setdefault(e["category"], []).append(e)
    selfmade = by["aug_self_made_evidence"]
    assert selfmade and all(e["label"] == 1 for e in selfmade)
    texts = {t for _, t in a.SELF_MADE}
    for e in selfmade:   # the signal must be visible in what the model reads
        r = e["request"]
        blob = r["business_justification"] + " ".join(
            ev.get("description", "") for ev in r["evidence"])
        assert any(t in blob for t in texts)
    mm = by["aug_purpose_mismatch_multilingual"]
    assert mm and all(e["label"] == 1 and e["wording"] == "non_english" for e in mm)
    hn = by["aug_hard_negative_topic"]
    assert hn and all(e["label"] == 0 for e in hn)
    assert {e["split"] for e in selfmade + mm + hn} == {"train", "dev"}
