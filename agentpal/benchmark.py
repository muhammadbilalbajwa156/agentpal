"""AgentPal-Bench v1 harness (P0.8): lockable benchmark of single requests and
multi-request episodes replayed through the HTTP API.

Design (see benchmarks/agentpal-bench-v1/README.md):
* Every item carries `group_id`, `split` ("dev" or "locked"), a human label and rationale,
  its `author` and whether it was written independently of the rule authors.
* A scenario group may never appear in more than one split.
* The `locked` split is evaluated only when LOCK.json exists and every file still matches
  its recorded SHA-256; each locked evaluation is appended to access_log.jsonl.
* Rules-only, classifier-only and hybrid are scored on the IDENTICAL single cases.
  Episodes are stateful (ledger, replay, trust) and are replayed through the real API for
  rules-only and hybrid; a classifier alone has no stateful policy, so it is not run on
  episodes.

Results from this harness are only as independent as the items: the seed `dev` items in
this repository were written by the rule author and are NOT an independent benchmark.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from .analyzers import HeuristicAnalyzer, MLAnalyzer
from .api import create_app
from .config import Settings, load_policy
from .evaluation import binary_metrics, operational_metrics
from .features import AssessmentContext, extract_features, ml_vector
from .integrity import load_manifest, sha256_file
from .orchestrator import assess
from .schemas import PaymentRequest
from .service import RiskService
from .spending import ApprovalTerms, SpendPolicy, SpendSnapshot
from .store import AssessmentStore

FILES = ("cases.jsonl", "episodes.jsonl")
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class BenchmarkError(RuntimeError):
    pass


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load(bench_dir: str | Path) -> tuple[list[dict], list[dict]]:
    d = Path(bench_dir)
    cases, episodes = _read_jsonl(d / "cases.jsonl"), _read_jsonl(d / "episodes.jsonl")
    seen: dict[str, str] = {}
    for item in cases + episodes:
        for key in ("id", "group_id", "split", "author", "independent"):
            if key not in item:
                raise BenchmarkError(f"{item.get('id', '?')}: missing {key}")
        if item["split"] not in ("dev", "locked"):
            raise BenchmarkError(f"{item['id']}: split must be dev or locked")
        prior = seen.setdefault(item["group_id"], item["split"])
        if prior != item["split"]:
            raise BenchmarkError(f"group {item['group_id']} appears in both {prior} and "
                                 f"{item['split']} (related variations must share a split)")
    return cases, episodes


def lock(bench_dir: str | Path, note: str = "") -> dict:
    d = Path(bench_dir)
    load(d)  # validate before locking
    record = {"locked_at": datetime.now(timezone.utc).isoformat(), "note": note,
              "files": {f: sha256_file(d / f) for f in FILES if (d / f).exists()}}
    (d / "LOCK.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _verify_lock(d: Path) -> dict:
    lock_file = d / "LOCK.json"
    if not lock_file.exists():
        raise BenchmarkError("locked split requested but LOCK.json is missing")
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    for name, digest in record["files"].items():
        if not (d / name).exists() or sha256_file(d / name) != digest:
            raise BenchmarkError(f"{name} changed after the benchmark was locked")
    return record


# ------------------------------------------------------------------ single cases

def _spend_for(case: dict, req: PaymentRequest) -> SpendSnapshot:
    op = case.get("operator", {})
    return SpendSnapshot.from_synthetic_fixture(op.get("monthly_budget", "1000.00"),
                                                op.get("committed", "0"), req.currency)


def _ml(settings: Settings) -> MLAnalyzer | None:
    ml = MLAnalyzer(settings.ml_model_path,
                    expected_sha256=load_manifest(settings.model_manifest_path).digest_for("ml"),
                    require_integrity=True)
    return ml if ml.available() else None


def run_cases(cases: list[dict], settings: Settings) -> dict:
    policy, ml = load_policy(settings.policy_path), _ml(settings)
    rows = []
    for case in cases:
        req = PaymentRequest.model_validate(case["request"])
        ctx = AssessmentContext(now=EPOCH, spend=_spend_for(case, req))
        row = {"id": case["id"], "label": case["label"],
               "rules_only": assess(req, ctx, policy, [HeuristicAnalyzer()]).recommendation}
        if ml is not None:
            p, _ = ml.predict_proba(ml_vector(extract_features(req, ctx, policy)))
            row["classifier_only"] = ("require_review" if p >= ml._load()["threshold"]
                                      else "allow_for_review")
            row["hybrid"] = assess(req, ctx, policy, [ml]).recommendation
        rows.append(row)
    approaches = [a for a in ("rules_only", "classifier_only", "hybrid") if rows and a in rows[0]]
    y = [r["label"] for r in rows]
    return {
        "n": len(rows),
        "metrics": {a: binary_metrics(y, [int(r[a] != "allow_for_review") for r in rows])
                    for a in approaches},
        "operational": {a: operational_metrics(y, [r[a] for r in rows]) for a in approaches},
        "predictions": rows,  # identical examples for every approach
        "classifier_available": ml is not None,
    }


# ------------------------------------------------------------------- episodes

def _spend_policy(episode: dict) -> SpendPolicy:
    if "spend_policy" in episode:
        return SpendPolicy.model_validate(episode["spend_policy"])
    from .spending import load_spend_policy
    return load_spend_policy()


def replay_episode(episode: dict, chain, settings: Settings) -> list[dict]:
    """Replay one episode through the real FastAPI app with a controlled server clock."""
    clock = {"t": EPOCH}
    service = RiskService(load_policy(settings.policy_path), AssessmentStore(":memory:"),
                          chain, clock=lambda: clock["t"], spend_policy=_spend_policy(episode))
    results = []
    with TestClient(create_app(service=service, settings=settings)) as client:
        for i, step in enumerate(episode["steps"]):
            clock["t"] = clock["t"] + timedelta(seconds=step.get("advance_seconds", 0))
            resp = client.post("/assess", json=step["request"])
            if resp.status_code != 200:
                # an HTTP refusal is a failed step, and its follow-up operations cannot run
                results.append({"step": i, "passed": False, "recommendation": None,
                                "codes": [], "expected": step.get("expect", {}),
                                "http_status": resp.status_code,
                                "known_gap": step.get("known_gap", "")})
                continue
            body = resp.json()
            codes = {s["code"] for s in body.get("signals", [])}
            exp = step.get("expect", {})
            checks = []
            if "action" in exp:
                checks.append(body.get("recommendation") == exp["action"])
            if "action_not" in exp:
                checks.append(body.get("recommendation") != exp["action_not"])
            for code in exp.get("codes", []):
                checks.append(code in codes)
            for code in exp.get("not_codes", []):
                checks.append(code not in codes)
            results.append({"step": i, "passed": all(checks), "recommendation":
                            body.get("recommendation"), "codes": sorted(codes),
                            "expected": exp, "known_gap": step.get("known_gap", "")})
            for op in step.get("then", []):
                rid = step["request"]["request_id"]
                # the owner approves exactly the terms of the request that was assessed
                terms = ApprovalTerms.from_request(PaymentRequest.model_validate(step["request"]))
                if op["op"] == "owner_decision":
                    service.store.record_owner_decision(rid, op["decision"], "bench-owner",
                                                        terms=terms)
                elif op["op"] == "payment_attempt":
                    service.store.record_payment_attempt(rid, "bench", f"att-{rid}",
                                                         terms=terms)
                elif op["op"] == "payment_outcome":
                    service.store.record_payment_outcome(
                        rid, op["outcome"], verified=op.get("verified", True),
                        verification_method="bench-fixture", provider_reference=f"cap-{rid}")
    return results


def run_episodes(episodes: list[dict], settings: Settings) -> dict:
    chains = {"rules_only": lambda: [HeuristicAnalyzer()]}
    if _ml(settings) is not None:
        chains["hybrid"] = lambda: [_ml(settings)]
    out = {}
    for name, make_chain in chains.items():
        per = {e["id"]: replay_episode(e, make_chain(), settings) for e in episodes}
        steps = [s for res in per.values() for s in res]
        out[name] = {"steps_passed": sum(s["passed"] for s in steps), "steps": len(steps),
                     "episodes_fully_passed": sum(all(s["passed"] for s in r)
                                                  for r in per.values()),
                     "episodes": len(per), "detail": per}
    return out


def evaluate_benchmark(bench_dir: str | Path, split: str, report_path: str | Path,
                       settings: Settings | None = None) -> dict:
    d = Path(bench_dir)
    # In-process, loopback-only replay that measures risk logic, not authentication, so it
    # runs in the explicit dev auth mode (authentication is tested in tests/test_sec_*.py).
    settings = settings or Settings(analyzer_chain=["ml", "heuristic"], db_path=":memory:",
                                    auth_mode="disabled_insecure_dev")
    cases, episodes = load(d)
    lock_record = None
    if split == "locked":
        lock_record = _verify_lock(d)
    cases = [c for c in cases if c["split"] == split]
    episodes = [e for e in episodes if e["split"] == split]
    items = cases + episodes
    report = {
        "benchmark": d.name, "split": split, "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "lock": lock_record,
        "file_sha256": {f: sha256_file(d / f) for f in FILES if (d / f).exists()},
        "independent_items": sum(bool(i["independent"]) for i in items), "items": len(items),
        "notice": ("Items not written independently of the rule authors cannot measure "
                   "generalization; treat such results as harness/regression checks only."),
        "cases": run_cases(cases, settings) if cases else None,
        "episodes": run_episodes(episodes, settings) if episodes else None,
    }
    if split == "locked":
        with (d / "access_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": report["evaluated_at"], "split": split,
                                 "report": str(report_path)}) + "\n")
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
