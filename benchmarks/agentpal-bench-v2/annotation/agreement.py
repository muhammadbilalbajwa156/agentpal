"""Inter-annotator agreement for AgentPal-Bench v2 (protocol section 5, steps 1 and 3).

    python benchmarks/agentpal-bench-v2/annotation/agreement.py \
        --bench-dir <dir with cases.jsonl/episodes.jsonl> \
        --annotations ann_a.jsonl ann_b.jsonl ann_c.jsonl --out agreement.json

Reports Fleiss' kappa (all raters) and pairwise Cohen's kappa for `risky` and for
`expected_action`, overall and per risk type. The gate is kappa >= 0.6 on `risky`
(protocol: below that, revise the guideline and re-pilot; a risk type below 0.6 is
reported but excluded from headline metrics).

Fleiss' kappa uses the standard formula generalized to a varying number of raters per
item (each item needs >= 2 ratings). Kappa is undefined (null) when every rating falls in
one category (expected agreement = 1); the report says so instead of printing 1.0.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annolib import ACTIONS, by_item, load_annotations, units  # noqa: E402

KAPPA_GATE = 0.6


def fleiss_kappa(ratings: list[list[object]], categories: list[object] | None = None
                 ) -> float | None:
    """ratings[i] = the category each rater gave item i (>= 2 ratings per item)."""
    items = [r for r in ratings if len(r) >= 2]
    if not items:
        return None
    cats = list(categories) if categories else sorted({c for r in items for c in r}, key=str)
    total = sum(len(r) for r in items)
    p_bar = 0.0
    col = Counter()
    for r in items:
        counts = Counter(r)
        col.update(counts)
        n = len(r)
        p_bar += sum(c * (c - 1) for c in counts.values()) / (n * (n - 1))
    p_bar /= len(items)
    pe = sum((col[c] / total) ** 2 for c in cats)
    if abs(1 - pe) < 1e-12:
        return None
    return (p_bar - pe) / (1 - pe)


def cohen_kappa(a: list[object], b: list[object]) -> float | None:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if abs(1 - pe) < 1e-12:
        return None
    return (po - pe) / (1 - pe)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _block(rows_by_item: dict[str, list[dict]], item_ids: list[str]) -> dict:
    ids = [i for i in item_ids if len(rows_by_item.get(i, [])) >= 2]
    risky = [[r["risky"] for r in rows_by_item[i]] for i in ids]
    action = [[r["expected_action"] for r in rows_by_item[i]] for i in ids]
    k_risky = fleiss_kappa(risky, [0, 1])
    return {"items": len(ids),
            "ratings": sum(len(x) for x in risky),
            "fleiss_kappa_risky": _r(k_risky),
            "fleiss_kappa_expected_action": _r(fleiss_kappa(action, list(ACTIONS))),
            "raw_agreement_risky": _r(sum(len(set(x)) == 1 for x in risky) / len(ids))
            if ids else None,
            "passes_gate": (k_risky is not None and k_risky >= KAPPA_GATE)}


def agreement_report(bench_dir: str | Path, annotation_files: list[str | Path]) -> dict:
    rows = load_annotations(annotation_files)
    rb = by_item(rows)
    meta = {u["unit_id"]: u for u in units(bench_dir)}
    unknown = sorted(set(rb) - set(meta))
    if unknown:
        raise ValueError(f"annotations for unknown items: {unknown[:10]}")
    all_ids = sorted(rb)
    report = {"kappa_gate": KAPPA_GATE, "annotators": sorted({r["annotator_id"] for r in rows}),
              "guideline_versions": sorted({r["guideline_version"] for r in rows}),
              "overall": _block(rb, all_ids), "per_risk_type": {}, "pairwise_cohen": {}}
    for rt in sorted({meta[i]["risk_type"] for i in all_ids}):
        report["per_risk_type"][rt] = _block(rb, [i for i in all_ids
                                                  if meta[i]["risk_type"] == rt])
    for a, b in itertools.combinations(report["annotators"], 2):
        shared = [i for i in all_ids if {a, b} <= {r["annotator_id"] for r in rb[i]}]
        la = {r["item_id"]: r for r in rows if r["annotator_id"] == a}
        lb = {r["item_id"]: r for r in rows if r["annotator_id"] == b}
        report["pairwise_cohen"][f"{a}|{b}"] = {
            "items": len(shared),
            "risky": _r(cohen_kappa([la[i]["risky"] for i in shared],
                                    [lb[i]["risky"] for i in shared])),
            "expected_action": _r(cohen_kappa([la[i]["expected_action"] for i in shared],
                                              [lb[i]["expected_action"] for i in shared]))}
    report["excluded_from_headline_risk_types"] = sorted(
        rt for rt, b in report["per_risk_type"].items() if not b["passes_gate"])
    report["gate_passed"] = report["overall"]["passes_gate"]
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fleiss/Cohen kappa for v2 annotations")
    p.add_argument("--bench-dir", required=True)
    p.add_argument("--annotations", nargs="+", required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    rep = agreement_report(args.bench_dir, args.annotations)
    text = json.dumps(rep, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    o = rep["overall"]
    print(f"items={o['items']} kappa_risky={o['fleiss_kappa_risky']} "
          f"kappa_action={o['fleiss_kappa_expected_action']} gate_passed={rep['gate_passed']}")
    if rep["excluded_from_headline_risk_types"]:
        print("risk types below the gate:", ", ".join(rep["excluded_from_headline_risk_types"]))
    return 0 if rep["gate_passed"] else 3


if __name__ == "__main__":
    sys.exit(main())
