"""Baseline fitting and reproducible evaluation on the synthetic dataset.

All metrics produced here are SYNTHETIC-EVALUATION results: they show the
pipeline behaves as designed on generated scenarios, not real-world accuracy.
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import resource
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .analyzers import HeuristicAnalyzer, LlamaCppAnalyzer, MLAnalyzer, RiskAnalyzer
from .config import RiskPolicy, load_policy
from .dataset import load_split
from .features import ML_FEATURE_NAMES, AssessmentContext, extract_features, ml_vector
from .orchestrator import assess
from .integrity import sha256_file
from .schemas import PaymentRequest
from .spending import SpendSnapshot

ACTIONS = ["allow_for_review", "require_review", "block"]
# The synthetic test split was inspected during the Phase 1 review; it may be re-run as a
# regression check but must not be used to tune thresholds or select models (P0.8).
RETIRED_SPLITS = {"test": "retired_for_model_selection (regression check only)"}
SYNTHETIC_NOTICE = ("SYNTHETIC EVALUATION: generated data with hand-designed scenarios. "
                    "Not evidence of real-world fraud-detection accuracy.")


def to_inputs(example: dict) -> tuple[PaymentRequest, AssessmentContext]:
    """Synthetic examples carry their budget scenario in request fields. Offline only,
    those fields are mapped to an explicit *fixture* spend snapshot standing in for
    operator configuration; the live service never trusts them (P0.2)."""
    req = PaymentRequest.model_validate(example["request"])
    ctx = AssessmentContext.model_validate(example["context"])
    ctx.spend = SpendSnapshot.from_synthetic_fixture(
        req.monthly_budget, req.budget_spent_this_period, req.currency)
    # Fixture assumption (offline only): the scenario's prior rows for the same agent
    # represent verified completed payments. The live service derives trusted history
    # from verified payment outcomes only (P0.4).
    ctx.trusted_history = [h for h in ctx.history if h.agent_id == req.agent_id]
    return req, ctx


# ------------------------------------------------------------------ training

def fit_baseline(data_dir: str | Path, out_path: str | Path, seed: int = 0,
                 policy: RiskPolicy | None = None) -> dict:
    """Fit a logistic-regression baseline on train, select C/threshold on validation."""
    import numpy as np
    import sklearn
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import f1_score
    from sklearn.preprocessing import StandardScaler

    policy = policy or load_policy()

    def matrix(split: str):
        rows = load_split(data_dir, split)
        X, y = [], []
        for ex in rows:
            req, ctx = to_inputs(ex)
            X.append(ml_vector(extract_features(req, ctx, policy)))
            y.append(ex["label"])
        return np.array(X, dtype=float), np.array(y)

    Xtr, ytr = matrix("train")
    Xva, yva = matrix("validation")
    scaler = StandardScaler().fit(Xtr)
    best = None
    for C in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0):
        clf = LogisticRegression(C=C, class_weight="balanced", max_iter=2000,
                                 random_state=seed).fit(scaler.transform(Xtr), ytr)
        proba = clf.predict_proba(scaler.transform(Xva))[:, 1]
        for t in [i / 100 for i in range(10, 91, 5)]:
            f1 = f1_score(yva, proba >= t)
            if best is None or f1 > best[0] + 1e-9:
                best = (f1, C, t, clf)
    f1, C, threshold, clf = best
    artifact = {
        "type": "logistic_regression",
        "version": f"lr-{datetime.now(timezone.utc).strftime('%Y%m%d')}-C{C}",
        "feature_names": ML_FEATURE_NAMES,
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coef": clf.coef_[0].tolist(),
        "intercept": float(clf.intercept_[0]),
        "threshold": threshold,
        "selected_on": {"split": "validation", "f1": round(float(f1), 4), "C": C},
        "trained_on": {"split": "train", "n": int(len(ytr)),
                       "positives": int(ytr.sum())},
        "sklearn_version": sklearn.__version__,
        "notice": "Trained on synthetic data only.",
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return artifact


# ---------------------------------------------------------------- evaluation

def binary_metrics(y_true: list[int], y_pred: list[int]) -> dict:
    tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 1 and p == 1)
    tn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 0 and p == 0)
    fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 1 and p == 0)

    def div(a: float, b: float) -> float | None:
        return round(a / b, 4) if b else None

    precision, recall = div(tp, tp + fp), div(tp, tp + fn)
    f1 = (round(2 * precision * recall / (precision + recall), 4)
          if precision and recall else 0.0)
    return {"n": len(y_true), "precision": precision, "recall": recall, "f1": f1,
            "false_positive_rate": div(fp, fp + tn), "false_negative_rate": div(fn, fn + tp),
            "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
            # 95% Wilson score intervals: small samples get honestly wide intervals (P0.8)
            "ci95": {"precision": wilson_interval(tp, tp + fp),
                     "recall": wilson_interval(tp, tp + fn),
                     "false_positive_rate": wilson_interval(fp, fp + tn)}}


def wilson_interval(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def action_confusion(expected: list[str], predicted: list[str]) -> dict:
    matrix = {e: {p: 0 for p in ACTIONS} for e in ACTIONS}
    for e, p in zip(expected, predicted, strict=True):
        matrix[e][p] += 1
    correct = sum(matrix[a][a] for a in ACTIONS)
    return {"rows=expected,cols=predicted": matrix,
            "exact_action_accuracy": round(correct / len(expected), 4) if expected else None}


def _per_category(rows: list[dict], preds: list[int]) -> dict:
    out: dict[str, dict] = {}
    for ex, p in zip(rows, preds, strict=True):
        c = out.setdefault(ex["category"], {"label": ex["label"], "n": 0, "flagged": 0})
        c["n"] += 1
        c["flagged"] += p
    for c in out.values():
        rate = round(c["flagged"] / c["n"], 4)
        c["detection_rate" if c["label"] == 1 else "false_positive_rate"] = rate
    return dict(sorted(out.items()))


def operational_metrics(labels: list[int], actions: list[str],
                        degraded: list[bool] | None = None) -> dict:
    """Owner decision 1: report missed risky requests, false positives, review load and
    blocks SEPARATELY (not only F1). Positive class = needs review or block."""
    n = len(actions)
    risky = [i for i, y in enumerate(labels) if y == 1]
    legit = [i for i, y in enumerate(labels) if y == 0]
    missed = sum(actions[i] == "allow_for_review" for i in risky)
    flagged_legit = sum(actions[i] != "allow_for_review" for i in legit)
    def rate(k: int, d: int) -> float | None:
        return round(k / d, 4) if d else None

    out = {
        "requests": n,
        "allow_rate": rate(actions.count("allow_for_review"), n),
        "review_rate": rate(actions.count("require_review"), n),
        "block_rate": rate(actions.count("block"), n),
        "risky_missed": missed, "risky_miss_rate": rate(missed, len(risky)),
        "risky_miss_rate_ci95": wilson_interval(missed, len(risky)),
        "legitimate_flagged": flagged_legit,
        "legitimate_flag_rate": rate(flagged_legit, len(legit)),
        "legitimate_flag_rate_ci95": wilson_interval(flagged_legit, len(legit)),
        "legitimate_blocked": sum(actions[i] == "block" for i in legit),
    }
    if degraded is not None:
        out["degraded_rate"] = rate(sum(degraded), n)
    return out


def _run_pipeline(rows: list[dict], chain: list[RiskAnalyzer], policy: RiskPolicy) -> dict:
    preds, actions, latencies, backends, degraded = [], [], [], {}, []
    for ex in rows:
        req, ctx = to_inputs(ex)
        t0 = time.perf_counter()
        a = assess(req, ctx, policy, chain)
        latencies.append((time.perf_counter() - t0) * 1000)
        actions.append(a.recommendation)
        preds.append(int(a.recommendation != "allow_for_review"))
        backends[a.model.backend] = backends.get(a.model.backend, 0) + 1
        degraded.append(a.degraded)
    y = [ex["label"] for ex in rows]
    return {
        "operational": operational_metrics(y, actions, degraded),
        "binary": binary_metrics(y, preds),
        "actions": action_confusion([ex["expected_action"] for ex in rows], actions),
        "per_category": _per_category(rows, preds),
        "latency_ms": _latency(latencies),
        "backend_usage": backends,
    }


def _latency(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {"mean": round(statistics.fmean(s), 3), "p50": round(s[len(s) // 2], 3),
            "p95": round(s[min(len(s) - 1, int(len(s) * 0.95))], 3), "max": round(s[-1], 3)}


def _ml_only(rows: list[dict], ml: MLAnalyzer, policy: RiskPolicy) -> dict:
    threshold = ml._load()["threshold"]
    preds, lat = [], []
    for ex in rows:
        req, ctx = to_inputs(ex)
        t0 = time.perf_counter()
        p, _ = ml.predict_proba(ml_vector(extract_features(req, ctx, policy)))
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append(int(p >= threshold))
    return {"binary": binary_metrics([ex["label"] for ex in rows], preds),
            "per_category": _per_category(rows, preds), "latency_ms": _latency(lat),
            "threshold": threshold}


def stratified_sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Up to n rows, spread evenly across categories (deterministic)."""
    rng = random.Random(seed)
    by_cat: dict[str, list[dict]] = {}
    for ex in sorted(rows, key=lambda e: e["example_id"]):
        by_cat.setdefault(ex["category"], []).append(ex)
    per = max(1, n // len(by_cat))
    out = []
    for cat in sorted(by_cat):
        out += rng.sample(by_cat[cat], min(per, len(by_cat[cat])))
    return out


def evaluate(data_dir: str | Path, ml_model_path: str | Path, report_path: str | Path,
             split: str = "test", llm_model_path: str | Path | None = None,
             llm_sample: int = 0, seed: int = 0, policy: RiskPolicy | None = None) -> dict:
    policy = policy or load_policy()
    rows = load_split(data_dir, split)
    manifest_path = Path(data_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    report: dict = {
        "notice": SYNTHETIC_NOTICE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agentpal_version": __version__,
        "rules_version": policy.rules_version,
        "dataset": {"split": split, "n": len(rows), "seed": manifest.get("seed"),
                    "sha256": manifest.get("sha256", {}).get(split),
                    "dataset_version": manifest.get("dataset_version")},
        "positive_class": "needs review/intervention (require_review or block)",
        "split_status": RETIRED_SPLITS.get(split, "in_use"),
        "environment": {"python": platform.python_version(), "machine": platform.machine(),
                        "processor_count": os.cpu_count()},
        "approaches": {},
    }
    approaches = report["approaches"]
    approaches["rules_only"] = {
        "description": "Deterministic rules + heuristic fallback (no model)",
        **_run_pipeline(rows, [HeuristicAnalyzer()], policy)}

    # Offline research tool: it evaluates the artifact it was given. Integrity is not
    # enforced here, but the exact artifact digest is recorded for provenance (P0.7d).
    ml = MLAnalyzer(ml_model_path, require_integrity=False)
    if Path(ml_model_path).exists():
        report["ml_artifact_sha256"] = sha256_file(ml_model_path)
    if ml.available():
        approaches["ml_classifier_only"] = {
            "description": "Logistic-regression baseline alone (threshold from validation)",
            "model_version": ml.model_version, **_ml_only(rows, ml, policy)}
        approaches["rules_plus_ml"] = {
            "description": "Orchestrator: rules + ML classifier (bounded blending)",
            "model_version": ml.model_version, **_run_pipeline(rows, [ml], policy)}
    else:
        report["ml_note"] = "ML artifact not found; run `python -m agentpal train` first."

    if llm_model_path and llm_sample > 0:
        llm = LlamaCppAnalyzer(llm_model_path, require_integrity=False)
        if Path(llm_model_path).exists():
            report["llm_artifact_sha256"] = sha256_file(llm_model_path)
        if llm.available():
            sample = stratified_sample(rows, llm_sample, seed)
            model_file = Path(llm_model_path)
            approaches["rules_only_on_llm_sample"] = {
                "description": "Rules only, on the same stratified sample as the LLM run",
                **_run_pipeline(sample, [HeuristicAnalyzer()], policy)}
            approaches["rules_plus_llm"] = {
                "description": f"Orchestrator: rules + {model_file.stem} via llama.cpp "
                             f"(stratified sample of {len(sample)})",
                "model_file_mb": round(model_file.stat().st_size / 1e6, 1),
                **_run_pipeline(sample, [llm], policy)}
        else:
            report["llm_note"] = "llama-cpp-python or model file unavailable; LLM not evaluated."

    report["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
