"""Build the supervised fine-tuning (SFT) dataset for the AgentPal risk-analysis model.

Design (see docs/RISK_MODEL_PLAN.md and docs/ML_AUDIT.md):
  * Inputs are produced by the SAME prompt builder the service uses
    (`agentpal.analyzers.prompting.build_messages`), with `include_rule_findings=False`, so
    the model learns to read the request rather than copy the deterministic rules. The
    serving configuration must use the same setting (LlamaCppAnalyzer
    include_rule_findings=False) and thinking disabled.
  * Targets are `ModelAnalysis` JSON. A target recommendation is never `block`: blocking
    belongs to the deterministic rules. Labels are the generator/augmentation author's
    labels, not independent ground truth.
  * Only examples whose label is VISIBLE in the model input are kept. Risks the prompt
    cannot show (an earlier identical payment in history, a replayed request id, an
    unknown previous reference, an unusual hour, a per-transaction limit) are the rules'
    job; training the model to "detect" them from an input that does not contain them
    teaches it to guess from shortcuts (ML_AUDIT ML-05). They are dropped and counted.
  * Splits are by TEXT-TEMPLATE FAMILY: a generator purpose or injection sentence, or an
    augmentation family (training/augment.py), belongs to exactly one of train/dev.
  * There is NO synthetic locked test. The locked test is the externally annotated
    benchmark (docs/BENCHMARK_ANNOTATION_PROTOCOL.md); it is never in this repository.
  * Fresh data is generated with a new seed; the retired `data/test.jsonl` is never read.
  * Claimed evidence presence is decorrelated from the label in train AND dev (claimed
    evidence is unverified, so it must not predict risk), and benign `note` evidence is
    added to legitimate requests so the evidence type is not a shortcut either.
  * Every SFT text is checked against agentpal-bench-v1/v2 (training/augment.py
    overlap_report); the build fails on any overlap.

    python -m training.sft_data --out build/sft --seed 7319
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
from dataclasses import asdict
from pathlib import Path

from agentpal.analyzers.base import AnalyzerInput
from agentpal.analyzers.prompting import build_messages
from agentpal.config import load_policy
from agentpal.dataset import DATASET_VERSION, generate
from agentpal.evaluation import to_inputs
from agentpal.features import extract_features
from agentpal.rules import evaluate_rules
from agentpal.schemas import ModelAnalysis, ModelFinding

from . import augment
from .audit import injection_text, spurious, template_key

DEFAULT_SEED = 7319          # NOT the seed of the shipped data/ (20260)
TRAIN_SHARE = 80             # percent of template families assigned to train
SALT = "agentpal-sft-v1"
INCLUDE_RULE_FINDINGS = False
SFT_VERSION = "sft-2"

# Categories whose label does not depend on evidence content: toggling claimed evidence
# there must not change the label (claimed evidence is unverified).
EVIDENCE_NEUTRAL = {"legit_low_risk", "legit_unusual", "excessive_amount",
                    "suspicious_domain", "unexpected_recurring", "purpose_mismatch",
                    "prompt_injection", "suspicious_combination"}

# Generator rows whose label the model input cannot show (ML_AUDIT ML-05).
INVISIBLE = {
    "duplicate": "the earlier identical payment / replayed id is only in history, which the "
                 "prompt does not show (rules: DUPLICATE_REQUEST_ID, duplicate intent)",
    "unknown_reference": "whether a previous_payment_reference exists is only known from "
                         "history",
    "odd_hours": "created_at is not part of the prompt",
    "over_review_limit": "the per-transaction review limit is operator configuration that "
                         "the prompt does not show",
    "over_hard_limit": "the per-transaction hard limit is not in the prompt (rules block)",
    "injection_text_lost": "generator bug: dataset._Builder.vary() re-draws the "
                           "justification, so some prompt_injection variants keep label 1 "
                           "but no longer contain any injection text (ML_AUDIT ML-06)",
}

FINDING = {
    "excessive_amount": "AMOUNT_OUT_OF_LINE",
    "suspicious_domain": "MERCHANT_IMPERSONATION",
    "unexpected_recurring": "UNEXPECTED_RECURRING",
    "purpose_mismatch": "PURPOSE_MISMATCH",
    "missing_or_inconsistent": "INCONSISTENT_REQUEST",
    "prompt_injection": "PROMPT_INJECTION",
    "suspicious_combination": "COMBINED_WEAK_SIGNALS",
}
DESCRIPTION = {
    "AMOUNT_OUT_OF_LINE": ["The amount does not fit the remaining operator budget.",
                           "Amount is large relative to what is left in the budget."],
    "MERCHANT_IMPERSONATION": ["The merchant name or domain imitates an approved merchant.",
                               "Domain looks like a variation of a known vendor's domain."],
    "UNEXPECTED_RECURRING": ["Recurring billing does not fit what is being bought.",
                             "The billing interval is unusual for this purchase."],
    "PURPOSE_MISMATCH": ["The purpose does not fit this merchant's business.",
                         "What is being bought is not something this vendor sells."],
    "INCONSISTENT_REQUEST": ["Information is missing or contradicts itself.",
                             "Details in the request do not agree with each other."],
    "PROMPT_INJECTION": ["Request text tries to instruct the assessor.",
                         "Agent-written text contains instructions aimed at the reviewer."],
    "COMBINED_WEAK_SIGNALS": ["Several weak signals appear together.",
                              "Unfamiliar merchant plus other weak signals."],
    "DUPLICATE_PAYMENT": ["The request says it repeats an earlier payment.",
                          "Text indicates this may be a second payment for the same bill."],
    "UNVERIFIED_SELF_MADE_EVIDENCE": [
        "The supporting document was made by the requester, not issued by the merchant.",
        "The invoice was produced internally, so it cannot support this payment."],
}
SUMMARY_RISKY = ["Needs a human look: {d}", "Review recommended. {d}",
                 "{d} A person should check this before money moves."]
SUMMARY_LEGIT = ["No concrete problem found; routine request.",
                 "Nothing in the request stands out as risky.",
                 "Purpose, merchant and amount are consistent; no issue found.",
                 "Looks routine. Claims were not verified, but no risk signal is visible."]
EVIDENCE_UNVERIFIED = "Claimed evidence has not been verified."


def bucket(text: str) -> str:
    h = int(hashlib.sha256(f"{SALT}:{text}".encode()).hexdigest(), 16) % 100
    return "train" if h < TRAIN_SHARE else "dev"


def split_of(ex: dict) -> str | None:
    # Purpose text and injection sentence decide the split of a generator row. The
    # generator has only five generic justification templates, too few to hold any out:
    # they are shared boilerplate across splits (documented limitation). Augmented rows
    # carry their own family split (training/augment.py).
    if "family" in ex:
        return ex["split"]
    purpose, _justification = template_key(ex)
    parts = [purpose]
    if (inj := injection_text(ex)) is not None:
        parts.append(inj)
    buckets = {bucket(p) for p in parts}
    return buckets.pop() if len(buckets) == 1 else None  # mixed -> dropped


def invisible_reason(ex: dict) -> str | None:
    if ex["category"] == "duplicate":
        return "duplicate"
    if ex["category"] == "prompt_injection" and injection_text(ex) is None:
        return "injection_text_lost"
    for key in ("unknown_reference", "odd_hours", "over_review_limit", "over_hard_limit"):
        if key in ex["scenario"]:
            return key
    return None


def _h(ex: dict, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{ex['example_id']}".encode()).hexdigest(), 16)


def toggle_evidence(ex: dict, rng: random.Random) -> dict:
    if "family" in ex or ex["category"] not in EVIDENCE_NEUTRAL:
        return ex
    ex = json.loads(json.dumps(ex))
    r = ex["request"]
    if rng.random() < 0.5:
        if r.get("evidence"):
            # keep injection text that lives in evidence notes (it defines the category)
            r["evidence"] = [e for e in r["evidence"] if injection_text({"request": e})]
        else:
            r["evidence"] = [{
                "type": rng.choice(["invoice", "quote", "receipt"]),
                "description": rng.choice(augment.INVOICE_DESCRIPTIONS),
                "reference": "INV-" + str(rng.randint(1000, 9999)),
                "amount": r["amount"], "currency": r["currency"],
                "merchant_domain": r.get("merchant_domain")}]
    if ex["label"] == 0 and rng.random() < 0.15:  # "note" evidence is not an injection cue
        r["evidence"].append({"type": "note", "description": rng.choice(augment.BENIGN_NOTES)})
    return ex


def _signal_evidence(ex: dict) -> str:
    r = ex["request"]
    cat = ex["category"]
    if cat == "prompt_injection":
        return injection_text(ex) or ""
    if cat == "suspicious_domain":
        return f"{r.get('merchant_name')} on {r.get('merchant_domain')}"
    if cat == "purpose_mismatch":
        return f"purpose '{r.get('purpose')}' with {r.get('merchant_name')}"
    if cat == "unexpected_recurring":
        return f"{r.get('billing_interval')} billing for '{r.get('purpose')}'"
    if cat == "excessive_amount":
        return f"amount {r.get('amount')} {r.get('currency')}"
    if cat == "missing_or_inconsistent":
        if not r.get("merchant_domain"):
            return "merchant_domain is missing"
        if (r.get("business_justification") or "").strip().lower() in ("", "n/a", "needed"):
            return "business_justification is empty or uninformative"
        return "claimed evidence amount differs from the request amount"
    return ""


def target_for(ex: dict) -> ModelAnalysis:
    """Grounded target: findings cite what is visible in the request; scores and wording
    vary deterministically so the model cannot memorise one string per category."""
    risky = ex["label"] == 1
    findings: list[ModelFinding] = []
    missing: list[str] = []
    if "family" in ex:  # augmented: explicit visible signals
        signals = [(c, e) for c, e in ex.get("signals", [])]
        missing += ex.get("missing", [])
    else:
        code = FINDING.get(ex["category"])
        signals = [(code, _signal_evidence(ex))] if risky and code else []
    for code, evidence in signals:
        options = DESCRIPTION[code]
        findings.append(ModelFinding(code=code,
                                     description=options[_h(ex, code) % len(options)],
                                     evidence=evidence[:300]))
    if ex["request"].get("evidence"):
        missing.append(EVIDENCE_UNVERIFIED)
    if risky:
        score = 55 + _h(ex, "score") % 31                       # 55..85
        uncertainty = 0.2 + (_h(ex, "unc") % 21) / 100          # 0.20..0.40
        if any(c == "INCONSISTENT_REQUEST" for c, _ in signals):
            uncertainty = 0.5 + (_h(ex, "unc") % 16) / 100      # missing info = uncertainty
        d = findings[0].description if findings else "Several weak signals together."
        summary = SUMMARY_RISKY[_h(ex, "sum") % len(SUMMARY_RISKY)].format(d=d)
    else:
        score = 3 + _h(ex, "score") % 18                        # 3..20
        uncertainty = 0.1 + (_h(ex, "unc") % 21) / 100          # 0.10..0.30
        summary = SUMMARY_LEGIT[_h(ex, "sum") % len(SUMMARY_LEGIT)]
    return ModelAnalysis(
        risk_score=score, findings=findings, missing_information=missing[:12],
        uncertainty=round(uncertainty, 2),
        recommendation="require_review" if risky else "allow_for_review",  # never block
        summary=summary[:300])


def to_record(ex: dict, policy) -> dict:
    req, ctx = to_inputs(ex)
    f = extract_features(req, ctx, policy)
    messages = build_messages(AnalyzerInput(req, f, evaluate_rules(f, policy)),
                              include_rule_findings=INCLUDE_RULE_FINDINGS)
    return {"id": ex["example_id"], "group_id": ex["group_id"], "category": ex["category"],
            "label": ex["label"], "family": ex.get("family", f"gen:{template_key(ex)[0]}"),
            "language": ex.get("language", "en"), "wording": ex.get("wording", "templated"),
            "source": ex.get("source", f"agentpal.dataset {DATASET_VERSION}"),
            "messages": messages, "target": target_for(ex).model_dump_json()}


def _seen_rate(records: list[dict]) -> dict:
    """Label rate by the `merchant_seen_in_previous_requests` fact shown to the model."""
    out = {}
    for val in (True, False):
        sel = [r for r in records
               if f'"merchant_seen_in_previous_requests": {str(val).lower()}'
               in r["messages"][1]["content"]]
        out[str(val).lower()] = {"n": len(sel), "risky_rate":
                                 round(sum(r["label"] for r in sel) / len(sel), 3)
                                 if sel else None}
    return out


def _counts(rows: list[dict], key: str) -> dict:
    return dict(sorted(collections.Counter(r[key] for r in rows).items()))


def build(out_dir: str | Path, seed: int = DEFAULT_SEED, with_augment: bool = True,
          check_overlap: bool = True) -> dict:
    policy = load_policy()
    rng = random.Random(seed)
    raw = [asdict(e) for e in generate(seed=seed, policy=policy)]
    by_split: dict[str, list[dict]] = {"train": [], "dev": []}
    raw_generator_train: list[dict] = []   # generator rows as generated (the "before")
    dropped_mixed, invisible = 0, collections.Counter()
    for ex in raw:
        s = split_of(ex)
        if s is None:
            dropped_mixed += 1
            continue
        if s == "train":
            raw_generator_train.append(ex)
        if (why := invisible_reason(ex)) is not None:
            invisible[why] += 1
            continue
        by_split[s].append(toggle_evidence(ex, rng))
    aug = augment.generate(seed) if with_augment else []
    for ex in aug:
        by_split[ex["split"]].append(ex)
    # a group never crosses splits
    groups = {s: {e["group_id"] for e in rows} for s, rows in by_split.items()}
    crossing = groups["train"] & groups["dev"]
    by_split["dev"] = [e for e in by_split["dev"] if e["group_id"] not in crossing]
    overlap = gen_v1 = None
    if check_overlap:
        # Hard gate: nothing may overlap bench v2 (the dev benchmark used for checkpoint
        # choice), and no augmented text may overlap bench v1 either. Generator rows DO
        # overlap bench v1, which the rule author seeded from the same templates
        # (leakage_check.py reports it as informational): bench v1 therefore cannot
        # evaluate a model trained on this data and is a regression check only.
        # The rule author's adversarial test strings were also derived from the generator,
        # so they are part of the reference for augmented text only.
        everything = by_split["train"] + by_split["dev"]
        overlap = augment.overlap_report(everything, benches=("agentpal-bench-v2",),
                                         include_adversarial_tests=False)
        aug_rows = [e for e in everything if "family" in e]
        aug_v1 = augment.overlap_report(aug_rows)  # v1 + v2 + adversarial test strings
        for rep in (overlap, aug_v1):
            if not rep["passed"]:
                raise ValueError(f"SFT text overlaps {rep['benchmarks']}: "
                                 f"{rep['findings_by_type']}; first: {rep['findings'][:3]}")
        gen_rows = [e for e in everything if "family" not in e]
        gen_v1 = augment.overlap_report(gen_rows, benches=("agentpal-bench-v1",),
                                        include_adversarial_tests=False)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files, records = {}, {}
    for s, rows in by_split.items():
        recs = [to_record(e, policy) for e in rows]
        records[s] = recs
        path = out / f"{s}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                        encoding="utf-8")
        files[s] = {"rows": len(rows), "risky": sum(e["label"] for e in rows),
                    "risky_share": round(sum(e["label"] for e in rows) / len(rows), 3),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "categories": _counts(rows, "category"),
                    "label_by_category": {c: sorted({e["label"] for e in rows
                                                     if e["category"] == c})
                                          for c in sorted({e["category"] for e in rows})},
                    "languages": _counts(recs, "language"),
                    "wording": _counts(recs, "wording"),
                    "sources": _counts(recs, "source"),
                    "families": len({r["family"] for r in recs}),
                    "distinct_targets": len({r["target"] for r in recs})}
    fam = {s: {r["family"] for r in records[s]} for s in records}
    manifest = {
        "builder": "training.sft_data", "sft_version": SFT_VERSION,
        "generator_dataset_version": DATASET_VERSION,
        "augment_version": augment.AUGMENT_VERSION if with_augment else None,
        "seed": seed, "salt": SALT, "train_share_of_template_families": TRAIN_SHARE,
        "include_rule_findings": INCLUDE_RULE_FINDINGS,
        "thinking": "disabled (enable_thinking=False at training and serving)",
        "dropped_mixed_template_examples": dropped_mixed,
        "dropped_label_not_visible_in_model_input": dict(sorted(invisible.items())),
        "dropped_label_not_visible_reasons": INVISIBLE,
        "groups_dropped_from_dev_for_crossing": len(crossing),
        "families_shared_by_train_and_dev": len(fam["train"] & fam["dev"]),
        "files": files,
        "locked_test": "none in this repository: the locked test is the externally "
                       "annotated benchmark (docs/BENCHMARK_ANNOTATION_PROTOCOL.md)",
        "retired_test_split_read": False,
        # before = the generator's train rows exactly as generated; after = what is written
        "spurious_correlation_train_before_debias": spurious(raw_generator_train),
        "spurious_correlation_train_after_debias": spurious(by_split["train"]),
        "spurious_correlation_dev_after_debias": spurious(by_split["dev"]),
        "merchant_seen_fact_vs_label": {s: _seen_rate(records[s]) for s in records},
        "benchmark_overlap": ({k: overlap[k] for k in ("passed", "findings_by_type",
                                                        "method")}
                              | {"trigram_overlap_fraction":
                                 overlap["stats"]["trigram_overlap_fraction"],
                                 "max_containment": overlap["stats"]["max_containment_max"],
                                 "reference_sources": overlap["reference_sources"]}
                              | {"augmented_vs_bench_v1_passed": aug_v1["passed"],
                                 "augmented_reference": "bench v1 + v2 + adversarial "
                                                        "test strings",
                                 "generator_rows_vs_bench_v1_known_overlap": {
                                     "rows_with_findings": len({x["unit"] for x in
                                                                gen_v1["findings"]}),
                                     "findings_by_type": gen_v1["findings_by_type"],
                                     "consequence": "bench v1 is a regression check only; "
                                                    "it cannot evaluate this model"}}
                              if overlap else "not checked"),
        "notice": "Synthetic and hand-written, author-labelled data. Suitable for format and "
                  "behaviour training only; it cannot demonstrate real-world accuracy.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False)
                                       + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default="build/sft")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--no-augment", action="store_true")
    a = p.parse_args(argv)
    m = build(a.out, a.seed, with_augment=not a.no_augment)
    print(json.dumps({k: m[k] for k in ("files", "dropped_label_not_visible_in_model_input",
                                        "families_shared_by_train_and_dev",
                                        "benchmark_overlap")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
