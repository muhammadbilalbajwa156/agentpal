"""Reproducible audit of the synthetic training data (risk-model milestone).

Measures what limits any model trained on `data/*.jsonl`:
  * template reuse: how few distinct purposes / justifications / merchants exist, and how
    many validation/test rows reuse a training template (memorization, not generalization);
  * effective sample size: rows vs independent scenario groups;
  * labels: whether each category has one fixed label (labels mirror the author's design);
  * spurious correlations: signals whose presence predicts the label only because of how
    the generator was written (evidence presence, recurring, ...);
  * memorization gap: rules / hybrid metrics on validation rows whose templates also occur
    in training vs rows whose templates do not.

The retired test split is only *counted* (overlap statistics); it is never used to fit or
select anything.

    python -m training.audit --data data --report reports/data_audit.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from agentpal.dataset import INJECTIONS_OBVIOUS, INJECTIONS_SUBTLE, PROJECTS

SPLITS = ("train", "validation", "test")
_PROJECT_RE = re.compile(r"\b(" + "|".join(PROJECTS) + r")\b")
INJECTION_TEXTS = INJECTIONS_OBVIOUS + INJECTIONS_SUBTLE


def load(data_dir: str | Path) -> dict[str, list[dict]]:
    d = Path(data_dir)
    return {s: [json.loads(line) for line in (d / f"{s}.jsonl").read_text().splitlines()
                if line.strip()] for s in SPLITS}


def template_key(ex: dict) -> tuple[str, str]:
    """(purpose, justification with project names masked): the generator's text template."""
    r = ex["request"]
    return (r.get("purpose") or "",
            _PROJECT_RE.sub("<P>", r.get("business_justification") or ""))


def injection_text(ex: dict) -> str | None:
    blob = json.dumps(ex["request"], ensure_ascii=False)
    return next((t for t in INJECTION_TEXTS if t in blob), None)


def diversity(rows: list[dict]) -> dict:
    out = {"rows": len(rows), "scenario_groups": len({e["group_id"] for e in rows})}
    for f in ("purpose", "business_justification", "merchant_name", "merchant_domain"):
        out[f"distinct_{f}"] = len({e["request"].get(f) for e in rows})
    out["distinct_text_templates"] = len({template_key(e) for e in rows})
    out["distinct_injection_sentences"] = len({t for e in rows if (t := injection_text(e))})
    return out


def overlap(data: dict[str, list[dict]]) -> dict:
    train_t = {template_key(e) for e in data["train"]}
    train_p = {e["request"]["purpose"] for e in data["train"]}
    train_inj = {t for e in data["train"] if (t := injection_text(e))}
    out = {}
    for s in ("validation", "test"):
        rows = data[s]
        inj = [t for e in rows if (t := injection_text(e))]
        out[s] = {
            "rows": len(rows),
            "template_seen_in_train": sum(template_key(e) in train_t for e in rows),
            "purpose_seen_in_train": sum(e["request"]["purpose"] in train_p for e in rows),
            "injection_rows": len(inj),
            "injection_sentence_seen_in_train": sum(t in train_inj for t in inj),
        }
    return out


def labels_by_category(rows: list[dict]) -> dict:
    c: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for e in rows:
        c[e["category"]][e["label"]] += 1
    return {k: {"labels": dict(v), "single_fixed_label": len(v) == 1} for k, v in sorted(c.items())}


def _rate(rows: list[dict], pred) -> dict:
    sel = [e for e in rows if pred(e)]
    return {"n": len(sel), "risky_rate": round(sum(e["label"] for e in sel) / len(sel), 3)
            if sel else None}


def spurious(rows: list[dict]) -> dict:
    """Label rate conditional on signals that should carry little or no information."""
    has_ev = lambda e: bool(e["request"].get("evidence"))  # noqa: E731
    out = {
        "base_risky_rate": round(sum(e["label"] for e in rows) / len(rows), 3),
        "evidence_present": _rate(rows, has_ev),
        "evidence_absent": _rate(rows, lambda e: not has_ev(e)),
        "recurring": _rate(rows, lambda e: e["request"].get("recurring")),
        "not_recurring": _rate(rows, lambda e: not e["request"].get("recurring")),
    }
    by_cat = collections.defaultdict(lambda: [0, 0])
    for e in rows:
        by_cat[e["category"]][0] += 1
        by_cat[e["category"]][1] += has_ev(e)
    out["evidence_present_share_by_category"] = {
        k: round(ev / n, 3) for k, (n, ev) in sorted(by_cat.items())}
    return out


def memorization_gap(data: dict[str, list[dict]], ml_model_path: str | Path | None) -> dict:
    """Rules / hybrid on validation rows with a training template vs without one."""
    from agentpal.analyzers import HeuristicAnalyzer, MLAnalyzer
    from agentpal.config import load_policy
    from agentpal.evaluation import _run_pipeline

    policy = load_policy()
    train_t = {template_key(e) for e in data["train"]}
    seen = [e for e in data["validation"] if template_key(e) in train_t]
    unseen = [e for e in data["validation"] if template_key(e) not in train_t]
    chains = {"rules_only": [HeuristicAnalyzer()]}
    if ml_model_path and Path(ml_model_path).exists():
        chains["rules_plus_ml"] = [MLAnalyzer(ml_model_path, require_integrity=False)]
    out = {}
    for subset, rows in (("template_seen_in_train", seen), ("template_unseen", unseen)):
        out[subset] = {"rows": len(rows), "risky": sum(e["label"] for e in rows)}
        for name, chain in chains.items():
            r = _run_pipeline(rows, chain, policy)
            o = r["operational"]
            out[subset][name] = {k: o[k] for k in (
                "risky_missed", "risky_miss_rate", "risky_miss_rate_ci95",
                "legitimate_flagged", "legitimate_flag_rate", "legitimate_flag_rate_ci95",
                "review_rate")}
    return out


def audit(data_dir: str | Path, ml_model_path: str | Path | None = None) -> dict:
    data = load(data_dir)
    rows = data["train"] + data["validation"] + data["test"]
    return {
        "notice": "Synthetic data audit. The retired test split is only counted, never "
                  "used for fitting or selection.",
        "diversity": {s: diversity(data[s]) for s in SPLITS} | {"all": diversity(rows)},
        "cross_split_overlap": overlap(data),
        "labels_by_category": labels_by_category(rows),
        "spurious_correlations_train": spurious(data["train"]),
        "memorization_gap_validation": memorization_gap(data, ml_model_path),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default="data")
    p.add_argument("--ml-model", default="artifacts/baseline_classifier.json")
    p.add_argument("--report", default="reports/data_audit.json")
    a = p.parse_args(argv)
    r = audit(a.data, a.ml_model)
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(r, indent=2) + "\n")
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
