"""Score approaches on AgentPal-Bench v2 items with labels from annotators (or, for a dev
smoke run only, the item author's proposals).

    # dev smoke run with the author's proposed labels (NOT a benchmark result)
    python benchmarks/agentpal-bench-v2/score.py --labels proposed \
        --out-dir benchmarks/agentpal-bench-v2/reports --name dev_smoke_self_labelled

    # real run: adjudicated labels from >= 3 annotators
    python benchmarks/agentpal-bench-v2/score.py --labels <adjudicated_labels.jsonl> ...

    # locked split (custodian only; the directory must be OUTSIDE the repository)
    python benchmarks/agentpal-bench-v2/score.py --split locked --bench-dir /custodian/v2 \
        --labels /custodian/v2/adjudicated_labels.jsonl --out-dir /custodian/v2/reports

Approaches
  rules_only       deterministic rules + heuristic fallback (no model)
  hybrid           rules + the pinned ML classifier (advisory; can only add review)
  classifier_only  the ML classifier alone (single cases only; it has no stateful policy)
  <name>           external predictions: --predictions name=path.jsonl with rows
                   {"unit_id", "action", "latency_ms"?, "degraded"?}

Single cases are scored exactly as agentpal.benchmark.run_cases does (same EPOCH, same
fixture budget); episodes are replayed through the real FastAPI app with a controlled clock,
as agentpal.benchmark.replay_episode does, plus: HTTP status checks, per-step latency and
degraded flags, refused ledger operations recorded instead of crashing the run, and the v2
op extensions (advance_seconds, terms_override).

Metrics are reported SEPARATELY (owner decision 1), overall and per risk type and wording
slice, each with a Wilson 95% interval: missed risky (false negatives), legitimate flagged
(false positives), legitimate blocked, required blocks missed, review/block/allow rates,
degraded rate, and p50/p95 latency. Rules-only vs rules+ML uses McNemar's exact test on the
same units with an Agresti-Min 95% interval for the paired difference.

A result is labelled INDEPENDENT only if every scored unit is `independent: true` AND has an
adjudicated label from >= 3 annotators. Otherwise the report says NOT INDEPENDENT, and a
`--labels proposed` run is labelled a DEV SMOKE RUN with SELF-LABELLED proposals.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent            # .../agentpal
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HERE / "annotation"))

from annolib import ACTIONS, read_jsonl, units as bench_units, write_jsonl  # noqa: E402

FLAGGED = ("require_review", "block")
MAX_LOCKED_RUNS = 5
LOCKED_LABELS = "adjudicated_labels.jsonl"


# ------------------------------------------------------------------ statistics

def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def rate(k: int, n: int) -> dict:
    return {"count": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 1])."""
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 3)


def mcnemar(a_ok: list[bool], b_ok: list[bool]) -> dict:
    """Exact two-sided McNemar test on paired outcomes, plus the Agresti-Min adjusted
    Wald 95% interval for P(B correct) - P(A correct)."""
    assert len(a_ok) == len(b_ok)
    n = len(a_ok)
    only_a = sum(x and not y for x, y in zip(a_ok, b_ok, strict=True))
    only_b = sum(y and not x for x, y in zip(a_ok, b_ok, strict=True))
    both = sum(x and y for x, y in zip(a_ok, b_ok, strict=True))
    neither = n - only_a - only_b - both
    d = only_a + only_b
    p = (min(1.0, 2 * sum(math.comb(d, i) for i in range(min(only_a, only_b) + 1)) / 2 ** d)
         if d else 1.0)
    n12, n21, nn = only_a + 0.5, only_b + 0.5, n + 2.0
    diff = (n21 - n12) / nn
    se = math.sqrt(max(0.0, (n12 + n21) - (n21 - n12) ** 2 / nn)) / nn
    ci = [round(diff - 1.96 * se, 4), round(diff + 1.96 * se, 4)]
    significant = p < 0.05 and (ci[0] > 0 or ci[1] < 0)
    return {"n": n, "both_correct": both, "neither_correct": neither,
            "only_a_correct": only_a, "only_b_correct": only_b,
            "difference_b_minus_a": round((only_b - only_a) / n, 4) if n else None,
            "difference_ci95_agresti_min": ci if n else None, "p_value_exact": round(p, 6),
            "significant_at_0_05_and_ci_excludes_0": significant}


# ------------------------------------------------------------------ labels

def load_labels(source: str, all_units: list[dict]) -> tuple[dict[str, dict], str]:
    if source == "proposed":
        return ({u["unit_id"]: {"risky": u["proposed_label"],
                                "expected_action": u["proposed_expected_action"],
                                "n_annotators": 0, "method": "author_proposal"}
                 for u in all_units if u["proposed_label"] is not None}, "proposed")
    labels = {}
    for r in read_jsonl(source):
        if r.get("risky") not in (0, 1) or r.get("expected_action") not in ACTIONS:
            raise ValueError(f"invalid adjudicated label row: {r}")
        labels[r["item_id"]] = {"risky": r["risky"], "expected_action": r["expected_action"],
                                "n_annotators": int(r.get("n_annotators", 0)),
                                "method": r.get("method", "unknown")}
    return labels, "adjudicated"


def independence(units: list[dict], labels: dict[str, dict], source: str) -> dict:
    reasons = []
    if source == "proposed":
        reasons.append("labels are the item author's own proposals (self-labelled), not "
                       "annotator labels")
    dependent = [u["unit_id"] for u in units if not u["independent"]]
    if dependent:
        reasons.append(f"{len(dependent)} of {len(units)} scored units are not "
                       "independent (independent: false)")
    weak = [u["unit_id"] for u in units
            if labels[u["unit_id"]]["n_annotators"] < 3
            or labels[u["unit_id"]]["method"] not in ("majority", "adjudicator")]
    if weak and source != "proposed":
        reasons.append(f"{len(weak)} scored units lack an adjudicated label from >= 3 "
                       "annotators")
    ok = not reasons
    return {"independent": ok,
            "status": ("INDEPENDENT (organisational independence; constructed scenarios)"
                       if ok else "NOT INDEPENDENT"),
            "reasons": reasons}


# ------------------------------------------------------------------ predictions

def _system():
    from agentpal import benchmark
    from agentpal.analyzers import HeuristicAnalyzer
    from agentpal.config import Settings, load_policy
    return benchmark, HeuristicAnalyzer, Settings, load_policy


def harness_settings(**kw):
    """Settings for the in-process, loopback-only replay. It measures risk logic, not
    authentication, so it uses the explicit dev auth mode when the Settings model has one
    (authentication is tested in tests/test_sec_*.py), never the process environment."""
    _, _, Settings, _ = _system()
    if "auth_mode" in Settings.model_fields:
        kw.setdefault("auth_mode", "disabled_insecure_dev")
    return Settings(db_path=":memory:", **kw)


def predict_cases(cases: list[dict], approaches: list[str]) -> dict[str, dict[str, dict]]:
    """Mirror of agentpal.benchmark.run_cases with timing and degraded flags."""
    benchmark, HeuristicAnalyzer, Settings, load_policy = _system()
    from agentpal.features import AssessmentContext, extract_features, ml_vector
    from agentpal.orchestrator import assess
    from agentpal.schemas import PaymentRequest
    settings = harness_settings()
    policy = load_policy(settings.policy_path)
    ml = benchmark._ml(settings) if {"hybrid", "classifier_only"} & set(approaches) else None
    out: dict[str, dict[str, dict]] = {a: {} for a in approaches}
    for c in cases:
        req = PaymentRequest.model_validate(c["request"])
        ctx = AssessmentContext(now=benchmark.EPOCH, spend=benchmark._spend_for(c, req))
        for a in approaches:
            t0 = time.perf_counter()
            if a == "rules_only":
                res = assess(req, ctx, policy, [HeuristicAnalyzer()])
                row = {"action": res.recommendation, "degraded": res.degraded}
            elif ml is None:
                continue
            elif a == "hybrid":
                res = assess(req, ctx, policy, [ml])
                row = {"action": res.recommendation, "degraded": res.degraded}
            elif a == "classifier_only":
                p, _ = ml.predict_proba(ml_vector(extract_features(req, ctx, policy)))
                row = {"action": "require_review" if p >= ml._load()["threshold"]
                       else "allow_for_review", "degraded": False}
            else:
                continue
            row["latency_ms"] = (time.perf_counter() - t0) * 1000
            out[a][c["id"]] = row
    return out


def replay_episode(episode: dict, chain, settings) -> dict:
    """agentpal.benchmark.replay_episode plus status checks, latency, degraded flags,
    recorded (not raised) ledger refusals and the v2 op extensions."""
    benchmark, *_ = _system()
    from fastapi.testclient import TestClient
    from agentpal.api import create_app
    from agentpal.config import load_policy
    from agentpal.schemas import PaymentRequest
    from agentpal.service import RiskService
    from agentpal.spending import ApprovalTerms
    from agentpal.store import AssessmentStore

    clock = {"t": benchmark.EPOCH}
    service = RiskService(load_policy(settings.policy_path), AssessmentStore(":memory:"),
                          chain, clock=lambda: clock["t"],
                          spend_policy=benchmark._spend_policy(episode))
    steps, ops = [], []
    with TestClient(create_app(service=service, settings=settings)) as client:
        for i, step in enumerate(episode["steps"]):
            adv = step.get("advance_seconds", 0)
            if adv < 0:
                raise ValueError(f"{episode['id']} step {i}: the clock cannot move backwards")
            clock["t"] += timedelta(seconds=adv)
            t0 = time.perf_counter()
            resp = client.post("/assess", json=step["request"])
            latency = (time.perf_counter() - t0) * 1000
            ok = resp.status_code == 200
            try:
                body = resp.json() if ok else {}
            except ValueError:
                body, ok = {}, False
            action = body.get("recommendation") if ok else None
            steps.append({"unit_id": f"{episode['id']}#{i}", "http_status": resp.status_code,
                          "action": action, "latency_ms": latency,
                          "degraded": bool(body.get("degraded", False)),
                          "codes": sorted({s["code"] for s in body.get("signals", [])})})
            if not ok:
                # an HTTP refusal is a failed unit (scored as a block and counted under
                # http_rejected); its ledger follow-ups cannot run and are recorded as such
                for j, op in enumerate(step.get("then", [])):
                    ops.append({"unit_id": f"{episode['id']}#{i}/op{j}", "op": op["op"],
                                "expected": op.get("proposed_op_outcome"),
                                "actual": "not_run", "error": f"HTTP {resp.status_code}"})
                continue
            req = PaymentRequest.model_validate(step["request"])
            for j, op in enumerate(step.get("then", [])):
                clock["t"] += timedelta(seconds=op.get("advance_seconds", 0))
                target = episode["steps"][op.get("target_step", i)]["request"]
                treq = PaymentRequest.model_validate(target) if "target_step" in op else req
                terms = ApprovalTerms.from_request(treq)
                if op.get("terms_override"):
                    terms = ApprovalTerms.model_validate({**terms.model_dump(),
                                                          **op["terms_override"]})
                rid = treq.request_id
                try:
                    if op["op"] == "owner_decision":
                        service.store.record_owner_decision(
                            rid, op["decision"], "bench-owner",
                            terms=terms if op["decision"] == "approved" else None)
                    elif op["op"] == "payment_attempt":
                        service.store.record_payment_attempt(rid, "bench", f"att-{rid}-{j}",
                                                             terms=terms)
                    elif op["op"] == "payment_outcome":
                        service.store.record_payment_outcome(
                            rid, op["outcome"], verified=op.get("verified", True),
                            verification_method="bench-fixture",
                            provider_reference=f"cap-{rid}")
                    outcome, error = "accepted", None
                except Exception as exc:  # a refusal is a result, not a harness crash
                    outcome, error = "refused", type(exc).__name__
                ops.append({"unit_id": f"{episode['id']}#{i}/op{j}", "op": op["op"],
                            "expected": op.get("proposed_op_outcome"), "actual": outcome,
                            "error": error})
    return {"steps": steps, "ops": ops}


def predict_episodes(episodes: list[dict], approaches: list[str]
                     ) -> tuple[dict[str, dict[str, dict]], dict[str, list[dict]]]:
    benchmark, HeuristicAnalyzer, Settings, _ = _system()
    settings = harness_settings()
    chains = {"rules_only": lambda: [HeuristicAnalyzer()]}
    if "hybrid" in approaches and benchmark._ml(settings) is not None:
        chains["hybrid"] = lambda: [benchmark._ml(settings)]
    preds, ops = {}, {}
    for name, make in chains.items():
        if name not in approaches:
            continue
        preds[name], ops[name] = {}, []
        for e in episodes:
            r = replay_episode(e, make(), settings)
            for s in r["steps"]:
                preds[name][s["unit_id"]] = s
            ops[name] += r["ops"]
    return preds, ops


def load_external(spec: str) -> tuple[str, dict[str, dict]]:
    name, path = spec.split("=", 1)
    rows = {}
    for r in read_jsonl(path):
        if r.get("action") not in ACTIONS:
            raise ValueError(f"{path}: invalid action in {r}")
        rows[r["unit_id"]] = {"action": r["action"], "latency_ms": r.get("latency_ms"),
                              "degraded": r.get("degraded")}
    return name, rows


# ------------------------------------------------------------------ metrics

def metrics(units: list[dict], labels: dict[str, dict], preds: dict[str, dict]) -> dict:
    scored = [u for u in units if u["unit_id"] in preds]
    act = {u["unit_id"]: preds[u["unit_id"]]["action"] for u in scored}
    risky = [u for u in scored if labels[u["unit_id"]]["risky"] == 1]
    legit = [u for u in scored if labels[u["unit_id"]]["risky"] == 0]
    must_block = [u for u in scored if labels[u["unit_id"]]["expected_action"] == "block"]
    n = len(scored)
    # an HTTP rejection (no recommendation) stops the payment: counted as a block, and
    # reported separately so it cannot hide a harness problem
    rejected = [u for u in scored if act[u["unit_id"]] is None]
    eff = {k: (v or "block") for k, v in act.items()}
    degraded = [preds[u["unit_id"]].get("degraded") for u in scored]
    lat = [preds[u["unit_id"]]["latency_ms"] for u in scored
           if preds[u["unit_id"]].get("latency_ms") is not None]
    confusion = {e: {p: 0 for p in ACTIONS} for e in ACTIONS}
    for u in scored:
        confusion[labels[u["unit_id"]]["expected_action"]][eff[u["unit_id"]]] += 1
    return {
        "units": n, "risky_units": len(risky), "legitimate_units": len(legit),
        "missed_risky_FN": rate(sum(eff[u["unit_id"]] == "allow_for_review" for u in risky),
                                len(risky)),
        "legitimate_flagged_FP": rate(sum(eff[u["unit_id"]] in FLAGGED for u in legit),
                                      len(legit)),
        "legitimate_blocked": rate(sum(eff[u["unit_id"]] == "block" for u in legit),
                                   len(legit)),
        "required_block_missed": rate(sum(eff[u["unit_id"]] != "block" for u in must_block),
                                      len(must_block)),
        "allow_rate": rate(sum(v == "allow_for_review" for v in eff.values()), n),
        "review_rate": rate(sum(v == "require_review" for v in eff.values()), n),
        "block_rate": rate(sum(v == "block" for v in eff.values()), n),
        "degraded_rate": (rate(sum(bool(d) for d in degraded), n)
                          if any(d is not None for d in degraded) else None),
        "http_rejected": len(rejected),
        "exact_action_match": rate(sum(eff[u["unit_id"]] == labels[u["unit_id"]]
                                       ["expected_action"] for u in scored), n),
        "latency_ms": {"p50": percentile(lat, 0.5), "p95": percentile(lat, 0.95),
                       "n": len(lat)},
        "action_confusion_rows_expected_cols_predicted": confusion,
    }


def sliced(units: list[dict], labels: dict, preds: dict, key: str) -> dict:
    return {v: metrics([u for u in units if u[key] == v], labels, preds)
            for v in sorted({u[key] for u in units})}


def compare(units: list[dict], labels: dict, a: dict, b: dict) -> dict:
    both = [u for u in units if u["unit_id"] in a and u["unit_id"] in b]

    def flagged(p: dict, uid: str) -> bool:
        return (p[uid]["action"] or "block") in FLAGGED

    def ok(p: dict, u: dict) -> bool:
        return flagged(p, u["unit_id"]) == (labels[u["unit_id"]]["risky"] == 1)

    risky = [u for u in both if labels[u["unit_id"]]["risky"] == 1]
    legit = [u for u in both if labels[u["unit_id"]]["risky"] == 0]
    return {
        "all_units_correct_decision": mcnemar([ok(a, u) for u in both],
                                              [ok(b, u) for u in both]),
        "risky_units_caught (recall)": mcnemar([flagged(a, u["unit_id"]) for u in risky],
                                              [flagged(b, u["unit_id"]) for u in risky]),
        "legitimate_units_not_flagged (1 - FPR)": mcnemar(
            [not flagged(a, u["unit_id"]) for u in legit],
            [not flagged(b, u["unit_id"]) for u in legit]),
    }


def op_checks(ops: list[dict]) -> dict:
    checked = [o for o in ops if o["expected"] in ("accepted", "refused")]
    wrong = [o for o in checked if o["expected"] != o["actual"]]
    return {"ops": len(ops), "checked": len(checked), "as_expected": len(checked) - len(wrong),
            "mismatches": wrong}


# ------------------------------------------------------------------ provenance

def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def system_provenance() -> dict:
    from agentpal.config import load_policy
    from agentpal.integrity import sha256_file
    s = harness_settings()
    ml = Path(s.ml_model_path)
    return {"commit": _git("rev-parse", "HEAD"),
            "working_tree_dirty": bool(_git("status", "--porcelain")),
            "rules_version": load_policy(s.policy_path).rules_version,
            "ml_artifact_sha256": sha256_file(ml) if ml.exists() else None,
            "python": sys.version.split()[0]}


def repo_root() -> Path:
    top = _git("rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else PROJECT_ROOT.resolve()


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ main flow

def score(bench_dir: str | Path, labels_source: str, split: str = "dev",
          approaches: tuple[str, ...] = ("rules_only", "hybrid", "classifier_only"),
          external: list[str] = (), compare_pair: tuple[str, str] = ("rules_only", "hybrid"),
          only_units: set[str] | None = None) -> dict:
    bench_dir = Path(bench_dir)
    all_units = bench_units(bench_dir)
    wrong_split = [u["unit_id"] for u in all_units if u["split"] != split]
    if wrong_split:
        # a dev run never reads locked items (and vice versa): keep the files separate
        raise SystemExit(f"refusing: {len(wrong_split)} units in {bench_dir} are not in "
                         f"split '{split}' (e.g. {wrong_split[:3]}); dev and locked items "
                         "must live in separate directories")
    labels, source = load_labels(labels_source, all_units)
    if split == "locked" and source == "proposed":
        raise SystemExit("refusing: the locked split must be scored with adjudicated labels")
    units = [u for u in all_units if u["unit_id"] in labels
             and (only_units is None or u["unit_id"] in only_units)]
    unlabelled = len(all_units) - len([u for u in all_units if u["unit_id"] in labels])
    cases_all, episodes_all = (read_jsonl(bench_dir / "cases.jsonl"),
                               read_jsonl(bench_dir / "episodes.jsonl"))
    wanted = {u["item_id"] for u in units}
    cases = [c for c in cases_all if c["id"] in wanted]
    episodes = [e for e in episodes_all if e["id"] in wanted]

    preds = predict_cases(cases, list(approaches))
    ep_preds, ep_ops = predict_episodes(episodes, list(approaches))
    for name, p in ep_preds.items():
        preds.setdefault(name, {}).update(p)
    for spec in external:
        name, rows = load_external(spec)
        preds[name] = rows
    preds = {k: v for k, v in preds.items() if v}

    indep = independence(units, labels, source)
    smoke = source == "proposed"
    banner = ("DEV SMOKE RUN - SELF-LABELLED (labels are the AI item author's proposals) - "
              "NOT INDEPENDENT - NOT A BENCHMARK RESULT" if smoke else indep["status"])
    report = {
        "banner": banner,
        "independence": indep,
        "split": split,
        "label_source": source,
        "benchmark_dir": bench_dir.name,
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "system": system_provenance(),
        "counts": {"units_scored": len(units), "unlabelled_units_skipped": unlabelled,
                   "single_cases": sum(u["kind"] == "case" for u in units),
                   "episode_steps": sum(u["kind"] == "episode_step" for u in units),
                   "episodes": len(episodes),
                   "risky": sum(labels[u["unit_id"]]["risky"] == 1 for u in units),
                   "legitimate": sum(labels[u["unit_id"]]["risky"] == 0 for u in units)},
        "notes": [
            "Positive class = needs review or block (risky = 1).",
            "Prevalence is set by design, not by real traffic; rates are not real-world "
            "fraud rates.",
            "classifier_only is scored on single cases only (no stateful policy).",
            "Single cases have no history and use the harness fixture limits (review 1000, "
            "hard 5000 in every currency).",
        ],
        "approaches": {},
        "comparisons": {},
        "ledger_operation_checks": {k: op_checks(v) for k, v in ep_ops.items()},
    }
    for name, p in preds.items():
        report["approaches"][name] = {
            "overall": metrics(units, labels, p),
            "single_cases": metrics([u for u in units if u["kind"] == "case"], labels, p),
            "episode_steps": metrics([u for u in units if u["kind"] == "episode_step"],
                                     labels, p),
            "per_risk_type": sliced(units, labels, p, "risk_type"),
            "per_wording": sliced(units, labels, p, "wording"),
            "per_language": sliced(units, labels, p, "language"),
        }
    a, b = compare_pair
    if a in preds and b in preds:
        report["comparisons"][f"{a} vs {b}"] = compare(units, labels, preds[a], preds[b])
    report["_predictions"] = {name: {uid: {"action": r["action"]} for uid, r in p.items()}
                              for name, p in preds.items()}
    return report


def aggregate_only(report: dict) -> dict:
    """Locked-split report for engineers: aggregates and failure categories, no item ids,
    texts or per-item predictions (protocol section 6.4)."""
    r = json.loads(json.dumps(report))
    r.pop("_predictions", None)
    for ops in r.get("ledger_operation_checks", {}).values():
        ops["mismatches"] = len(ops["mismatches"])
    return r


# ------------------------------------------------------------------ rendering

def _fmt(m: dict | None) -> str:
    if not m or m.get("n") in (None, 0):
        return "n/a"
    ci = m["ci95"]
    return f"{m['count']}/{m['n']} = {m['rate']:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"


def _table(rows: dict[str, dict], approach: str) -> str:
    out = ["| Slice | n | Missed risky (FN) | Legit flagged (FP) | Legit blocked | "
           "Review rate | Block rate |", "|---|---|---|---|---|---|---|"]
    for k, m in rows.items():
        out.append(f"| {k} | {m['units']} | {_fmt(m['missed_risky_FN'])} | "
                   f"{_fmt(m['legitimate_flagged_FP'])} | {_fmt(m['legitimate_blocked'])} | "
                   f"{_fmt(m['review_rate'])} | {_fmt(m['block_rate'])} |")
    return "\n".join(out)


def render_markdown(report: dict, template_path: Path = HERE / "report_template.md") -> str:
    t = template_path.read_text(encoding="utf-8")
    head = ["| Approach | Scope | n | Missed risky (FN) | Legit flagged (FP) | Legit blocked "
            "| Required block missed | Review rate | Block rate | Degraded | p50 / p95 ms |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, a in report["approaches"].items():
        for scope in ("overall", "single_cases", "episode_steps"):
            m = a[scope]
            if not m["units"]:
                continue
            lat = m["latency_ms"]
            head.append(f"| {name} | {scope} | {m['units']} | {_fmt(m['missed_risky_FN'])} | "
                        f"{_fmt(m['legitimate_flagged_FP'])} | "
                        f"{_fmt(m['legitimate_blocked'])} | "
                        f"{_fmt(m['required_block_missed'])} | {_fmt(m['review_rate'])} | "
                        f"{_fmt(m['block_rate'])} | {_fmt(m['degraded_rate'])} | "
                        f"{lat['p50']} / {lat['p95']} |")
    slices = []
    for name, a in report["approaches"].items():
        slices.append(f"### {name}: per risk type\n\n" + _table(a["per_risk_type"], name))
        slices.append(f"### {name}: per wording slice\n\n" + _table(a["per_wording"], name))
        slices.append(f"### {name}: per language\n\n" + _table(a["per_language"], name))
    comp = []
    for pair, c in report["comparisons"].items():
        comp.append(f"**{pair}** (B = second approach)\n\n| Question | n | only A right | "
                    "only B right | B - A | 95% CI (Agresti-Min) | exact p | claim? |\n"
                    "|---|---|---|---|---|---|---|---|")
        for q, m in c.items():
            comp.append(f"| {q} | {m['n']} | {m['only_a_correct']} | {m['only_b_correct']} | "
                        f"{m['difference_b_minus_a']} | {m['difference_ci95_agresti_min']} | "
                        f"{m['p_value_exact']} | "
                        f"{'yes' if m['significant_at_0_05_and_ci_excludes_0'] else 'no'} |")
    ops = [f"- {k}: {v['as_expected']}/{v['checked']} ledger operations behaved as the policy "
           f"requires" for k, v in report["ledger_operation_checks"].items()]
    sysinfo = report["system"]
    values = {
        "banner": report["banner"],
        "independence_status": report["independence"]["status"],
        "independence_reasons": "\n".join(f"- {r}" for r in report["independence"]["reasons"])
                                or "- none",
        "split": report["split"], "label_source": report["label_source"],
        "evaluated_at": report["evaluated_at"],
        "commit": str(sysinfo["commit"]), "dirty": str(sysinfo["working_tree_dirty"]),
        "rules_version": sysinfo["rules_version"],
        "ml_sha256": str(sysinfo["ml_artifact_sha256"]),
        "counts": "\n".join(f"- {k}: {v}" for k, v in report["counts"].items()),
        "headline_table": "\n".join(head),
        "slice_tables": "\n\n".join(slices),
        "comparisons": "\n".join(comp) or "_No paired comparison (an approach is missing)._",
        "ledger_ops": "\n".join(ops) or "- none",
        "notes": "\n".join(f"- {n}" for n in report["notes"]),
    }
    for k, v in values.items():
        t = t.replace("{{" + k + "}}", v)
    return t


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="score AgentPal-Bench v2")
    p.add_argument("--bench-dir", default=str(HERE))
    p.add_argument("--split", default="dev", choices=["dev", "locked"])
    p.add_argument("--labels", required=True,
                   help="'proposed' (dev smoke run only) or an adjudicated_labels.jsonl")
    p.add_argument("--approaches", default="rules_only,hybrid,classifier_only")
    p.add_argument("--predictions", action="append", default=[],
                   help="external approach: name=path.jsonl")
    p.add_argument("--compare", default="rules_only,hybrid")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--name", default="bench_v2_report")
    p.add_argument("--require-independent", action="store_true",
                   help="exit 2 unless the result qualifies as independent")
    args = p.parse_args(argv)
    bench = Path(args.bench_dir)
    out = Path(args.out_dir)
    if args.split == "locked":
        root = repo_root()
        if inside(bench, root) or inside(out, root):
            print(f"refusing: locked items and their outputs must live outside the "
                  f"repository ({root})", file=sys.stderr)
            return 2
        from agentpal import benchmark
        benchmark._verify_lock(bench)
        lock = json.loads((bench / "LOCK.json").read_text(encoding="utf-8"))
        # The labels must be THE locked file: <bench>/adjudicated_labels.jsonl, listed in
        # LOCK.json (its hash was just verified). Checking only the file NAME let a labels
        # file in a subdirectory, never locked or hashed, through (ML-11).
        labels_ok = (Path(args.labels).resolve() == (bench / LOCKED_LABELS).resolve()
                     and LOCKED_LABELS in lock["files"])
        missing = sorted(n for n in ("cases.jsonl", "episodes.jsonl")
                         if (bench / n).exists() and n not in lock["files"])
        if missing or not labels_ok:
            print(f"refusing: --labels must be <bench>/{LOCKED_LABELS} listed in LOCK.json "
                  f"and every item file must be locked (labels ok: {labels_ok}; unlocked: "
                  f"{missing})", file=sys.stderr)
            return 2
        runs = read_jsonl(bench / "access_log.jsonl")
        if len(runs) >= MAX_LOCKED_RUNS:
            print("refusing: this locked split has been evaluated 5 times and must be "
                  "retired (protocol section 6.4)", file=sys.stderr)
            return 2
    report = score(bench, args.labels, args.split, tuple(args.approaches.split(",")),
                   args.predictions, tuple(args.compare.split(",")))
    out.mkdir(parents=True, exist_ok=True)
    preds = report.pop("_predictions")
    if args.split == "locked":
        with (bench / "access_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": report["evaluated_at"], "split": "locked",
                                 **report["system"], "report": str(out / args.name)}) + "\n")
        write_jsonl(bench / "custodian_outputs" / f"{args.name}_predictions.jsonl",
                    [{"approach": a, "unit_id": u, **r} for a, rows in preds.items()
                     for u, r in sorted(rows.items())])
        report = aggregate_only(report)
    (out / f"{args.name}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)
                                           + "\n", encoding="utf-8")
    (out / f"{args.name}.md").write_text(render_markdown(report), encoding="utf-8")
    print(report["banner"])
    for name, a in report["approaches"].items():
        m = a["overall"]
        print(f"  {name:16s} n={m['units']} FN={_fmt(m['missed_risky_FN'])} "
              f"FP={_fmt(m['legitimate_flagged_FP'])} review={m['review_rate']['rate']} "
              f"block={m['block_rate']['rate']} p50/p95={m['latency_ms']['p50']}/"
              f"{m['latency_ms']['p95']}ms")
    print(f"report: {out / args.name}.json / .md")
    if args.require_independent and not report["independence"]["independent"]:
        print("NOT INDEPENDENT: " + "; ".join(report["independence"]["reasons"]),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
