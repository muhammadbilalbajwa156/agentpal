"""Build BLINDED annotation packets, one per annotator (protocol section 5, step 2).

    python benchmarks/agentpal-bench-v2/annotation/make_packets.py \
        --bench-dir <dir> --annotators ann01 ann02 ann03 --out <packets dir> \
        [--project-root <agentpal project root>]

Each annotator gets:
  units.jsonl            the items in an annotator-specific shuffled order, with ONLY what a
                         reviewer needs: the request, the trusted budget context, and (for
                         episodes) the ordered steps with elapsed time and ledger events;
  labels_template.jsonl  one row per labelable unit to fill in (schema: label_schema.json);
  context.json           owner policy summary: approved merchants, blocked domains,
                         allowed currencies, server time, and policy_clauses.json.

Removed on purpose: proposed labels/actions/rationales, risk_type, wording, language,
group_id, summary, mechanics, provenance, author notes and any AgentPal output. Packets
contain item text, so they are git-ignored (annotator_packets/) and, for the locked split,
must stay with the custodian.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annolib import STEP_SEP, load_bench, write_jsonl  # noqa: E402

HERE = Path(__file__).resolve().parent
GUIDE_FILES = ("ANNOTATOR_GUIDE.md", "worked_examples.jsonl")
SERVER_TIME = "2026-11-02T09:00:00Z"        # agentpal.benchmark.EPOCH
# The v1 harness scores single cases with these fixture limits for every currency
# (spending.SpendSnapshot.from_synthetic_fixture); annotators must see the same numbers.
CASE_FIXTURE_LIMITS = {"per_transaction_review_limit": "1000", "per_transaction_hard_limit":
                       "5000", "rolling_24h_limit": "equal to the monthly budget"}
HIDDEN_KEYS = {"proposed_label", "proposed_expected_action", "proposed_rationale",
               "proposed_policy_clause", "proposed_difficulty", "risk_type", "wording",
               "language", "group_id", "summary", "mechanics", "provenance", "author_note",
               "label_status", "author", "independent", "split", "harness_v1_compatible",
               "label", "rationale", "expect", "known_gap"}


def _event(op: dict) -> str:
    when = (f" {op['advance_seconds'] // 60} min later" if op.get("advance_seconds") else "")
    if op["op"] == "owner_decision":
        return f"Owner {op['decision']} this request{when}."
    if op["op"] == "payment_attempt":
        changed = op.get("terms_override")
        extra = (f" presenting CHANGED terms {json.dumps(changed, ensure_ascii=False)}"
                 if changed else " with the approved terms")
        return f"Agent submitted a payment attempt{when}{extra}."
    if op["op"] == "payment_outcome":
        v = "provider-verified" if op.get("verified", True) else "agent-reported, NOT verified"
        return f"Payment outcome reported{when}: {op['outcome']} ({v})."
    return op["op"]


def _policy_summary(sp: dict) -> dict:
    return {"agents": {a: {"currencies": v["currencies"]} for a, v in sp["agents"].items()},
            "approval_hold_minutes": sp.get("approval_validity_minutes", 15),
            "owner_shares_verified_merchant_history_across_agents":
                any(o.get("share_verified_merchant_history_across_agents")
                    for o in sp.get("owners", {}).values())}


def blinded_units(bench_dir: str | Path) -> list[dict]:
    cases, episodes = load_bench(bench_dir)
    out = []
    for c in cases:
        op = c.get("operator", {})
        out.append({"item_id": c["id"], "kind": "case", "request": c["request"],
                    "trusted_budget_context": {
                        "currency": c["request"]["currency"],
                        "monthly_budget": op.get("monthly_budget", "1000.00"),
                        "already_committed_this_month": op.get("committed", "0"),
                        **CASE_FIXTURE_LIMITS},
                    "history": "none (first request seen from this agent)"})
    for e in episodes:
        steps = []
        for i, s in enumerate(e["steps"]):
            steps.append({"step_item_id": f"{e['id']}{STEP_SEP}{i}",
                          "minutes_after_previous_step": round(s.get("advance_seconds", 0)
                                                               / 60, 2),
                          "request": s["request"],
                          "events_after_this_step": [_event(o) for o in s.get("then", [])]})
        out.append({"item_id": e["id"], "kind": "episode",
                    "spend_policy": _policy_summary(e.get("spend_policy", {"agents": {}})),
                    "steps": steps})
    for u in out:  # defence in depth: nothing on the hidden list may leak through
        assert not (HIDDEN_KEYS & set(u)), u["item_id"]
    return out


def make_packets(bench_dir: str | Path, annotators: list[str], out_dir: str | Path,
                 project_root: str | Path, seed: int = 0) -> dict:
    project_root = Path(project_root)
    policy = json.loads((project_root / "config" / "risk_policy.json").read_text("utf-8"))
    clauses = json.loads((HERE / "policy_clauses.json").read_text("utf-8"))
    context = {"server_time_for_single_cases": SERVER_TIME,
               "approved_merchants": policy["approved_merchants"],
               "blocked_domains": policy["blocked_domains"],
               "allowed_currencies": policy["limits"]["allowed_currencies"],
               "policy_clauses": clauses,
               "instructions": "Label against the owner policy only. Read "
                               "ANNOTATOR_GUIDE.md and worked_examples.jsonl in this "
                               "packet first."}
    base = blinded_units(bench_dir)
    out = Path(out_dir)
    manifest = {}
    for ann in annotators:
        order = random.Random(int(hashlib.sha256(f"{seed}:{ann}".encode()).hexdigest(), 16))
        mine = base[:]
        order.shuffle(mine)
        template = []
        for u in mine:
            ids = [u["item_id"]] if u["kind"] == "case" else [s["step_item_id"]
                                                              for s in u["steps"]]
            template += [{"annotator_id": ann, "item_id": i, "risky": None,
                          "expected_action": None, "rationale": "", "policy_clause": "",
                          "difficulty": None, "time_spent_seconds": None,
                          "guideline_version": ""} for i in ids]
        d = out / ann
        write_jsonl(d / "units.jsonl", mine)
        write_jsonl(d / "labels_template.jsonl", template)
        (d / "context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False)
                                        + "\n", encoding="utf-8")
        for name in GUIDE_FILES:  # the frozen guideline travels with every packet
            (d / name).write_bytes((HERE / name).read_bytes())
        manifest[ann] = {"units": len(mine), "label_rows": len(template)}
    return manifest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="blinded annotation packets")
    p.add_argument("--bench-dir", required=True)
    p.add_argument("--annotators", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--project-root", default=str(HERE.parent.parent.parent))
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    if len(set(args.annotators)) < 3:
        print("warning: the protocol requires >= 3 independent annotators per item",
              file=sys.stderr)
    print(json.dumps(make_packets(args.bench_dir, args.annotators, args.out,
                                  args.project_root, args.seed), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
