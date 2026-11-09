"""Produce final labels from >= 3 independent annotators (protocol section 5, step 4).

    python benchmarks/agentpal-bench-v2/annotation/adjudicate.py \
        --bench-dir <dir> --annotations a.jsonl b.jsonl c.jsonl \
        [--adjudications adjudicator.jsonl] --out-dir <dir>

Rules
  * An item needs labels from at least --min-annotators (default 3) distinct annotators;
    otherwise it is `insufficient` and gets no final label.
  * Final `risky` and `expected_action` are the strict majority (> half) of the votes.
  * The item goes to the adjudicator when either field has no strict majority (a tie), or
    when annotators disagree about `block` vs not-`block` (any split on that boundary), or
    when any annotator flagged it `ambiguous`.
  * An adjudicator row decides it (method "adjudicator"; rationale stored). An adjudicator
    row with "ambiguous": true moves it to ambiguous.jsonl, excluded from scoring.
  * Items awaiting an adjudicator are written to needs_adjudication.jsonl and get no final
    label until decided.

Adjudicator rows: {"item_id", "adjudicator_id", "risky", "expected_action", "rationale",
"ambiguous": false}. The adjudicator should not be an author of the item; if they also
annotated the item that is recorded (`adjudicator_was_annotator`).

Outputs in --out-dir: adjudicated_labels.jsonl, needs_adjudication.jsonl, ambiguous.jsonl,
insufficient.jsonl, adjudication_summary.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annolib import by_item, load_annotations, read_jsonl, strict_majority, units, write_jsonl  # noqa: E402,E501


def adjudicate(bench_dir: str | Path, annotation_files: list[str | Path],
               adjudication_file: str | Path | None = None, min_annotators: int = 3) -> dict:
    rows = load_annotations(annotation_files)
    rb = by_item(rows)
    known = {u["unit_id"] for u in units(bench_dir)}
    unknown = sorted(set(rb) - known)
    if unknown:
        raise ValueError(f"annotations for unknown items: {unknown[:10]}")
    adj = {r["item_id"]: r for r in (read_jsonl(adjudication_file) if adjudication_file
                                      else [])}
    for r in adj.values():
        if not r.get("ambiguous"):
            if r.get("risky") not in (0, 1) or r.get("expected_action") not in (
                    "allow_for_review", "require_review", "block"):
                raise ValueError(f"invalid adjudicator row for {r.get('item_id')}")
            if (r["risky"] == 0) != (r["expected_action"] == "allow_for_review"):
                raise ValueError(f"inconsistent adjudicator row for {r['item_id']}")
        if not r.get("adjudicator_id") or len(r.get("rationale", "")) < 10:
            raise ValueError(f"adjudicator row for {r.get('item_id')} needs adjudicator_id "
                             "and a rationale")
    out = {"final": [], "needs": [], "ambiguous": [], "insufficient": []}
    for item_id in sorted(known):
        votes = rb.get(item_id, [])
        annotators = sorted({v["annotator_id"] for v in votes})
        base = {"item_id": item_id, "annotators": annotators, "n_annotators": len(annotators),
                "votes": {"risky": dict(Counter(str(v["risky"]) for v in votes)),
                          "expected_action": dict(Counter(v["expected_action"]
                                                          for v in votes))},
                "guideline_versions": sorted({v["guideline_version"] for v in votes})}
        if len(annotators) < min_annotators:
            if votes:
                out["insufficient"].append(base)
            continue
        risky = strict_majority([v["risky"] for v in votes])
        action = strict_majority([v["expected_action"] for v in votes])
        reasons = []
        if risky is None:
            reasons.append("tie on risky")
        if action is None:
            reasons.append("tie on expected_action")
        blocks = [v["expected_action"] == "block" for v in votes]
        if any(blocks) and not all(blocks):
            reasons.append("block vs non-block disagreement")
        if any("ambiguous" in v.get("flags", []) for v in votes):
            reasons.append("flagged ambiguous by an annotator")
        if not reasons:
            out["final"].append({**base, "risky": risky, "expected_action": action,
                                 "method": "majority"})
            continue
        decision = adj.get(item_id)
        if decision is None:
            out["needs"].append({**base, "reasons": reasons})
        elif decision.get("ambiguous"):
            out["ambiguous"].append({**base, "reasons": reasons,
                                     "adjudicator_id": decision["adjudicator_id"],
                                     "adjudicator_rationale": decision["rationale"]})
        else:
            out["final"].append({**base, "risky": decision["risky"],
                                 "expected_action": decision["expected_action"],
                                 "method": "adjudicator", "reasons": reasons,
                                 "adjudicator_id": decision["adjudicator_id"],
                                 "adjudicator_was_annotator":
                                     decision["adjudicator_id"] in annotators,
                                 "adjudicator_rationale": decision["rationale"]})
    summary = {"items_in_benchmark": len(known), "items_annotated": len(rb),
               "min_annotators": min_annotators, "final_labels": len(out["final"]),
               "by_method": dict(Counter(r["method"] for r in out["final"])),
               "needs_adjudication": len(out["needs"]), "ambiguous": len(out["ambiguous"]),
               "insufficient_annotators": len(out["insufficient"]),
               "unannotated": len(known - set(rb))}
    return {"summary": summary, **out}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="majority + adjudicator labels for v2")
    p.add_argument("--bench-dir", required=True)
    p.add_argument("--annotations", nargs="+", required=True)
    p.add_argument("--adjudications", default=None)
    p.add_argument("--min-annotators", type=int, default=3)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    if args.min_annotators < 3:
        print("refusing: the protocol requires >= 3 annotators per item", file=sys.stderr)
        return 2
    res = adjudicate(args.bench_dir, args.annotations, args.adjudications,
                     args.min_annotators)
    d = Path(args.out_dir)
    write_jsonl(d / "adjudicated_labels.jsonl", res["final"])
    write_jsonl(d / "needs_adjudication.jsonl", res["needs"])
    write_jsonl(d / "ambiguous.jsonl", res["ambiguous"])
    write_jsonl(d / "insufficient.jsonl", res["insufficient"])
    (d / "adjudication_summary.json").write_text(json.dumps(res["summary"], indent=2) + "\n",
                                                 encoding="utf-8")
    print(json.dumps(res["summary"], indent=2))
    return 0 if not res["needs"] else 4


if __name__ == "__main__":
    sys.exit(main())
