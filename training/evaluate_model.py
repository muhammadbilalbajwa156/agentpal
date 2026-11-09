"""Post-training evaluation of the fine-tuned (or zero-shot) generative risk model.

    # (a) SFT dev split + (b) AgentPal-Bench v2 dev kit through its scorer
    python -m training.evaluate_model --adapter /kaggle/working/agentpal-lora/final \
        --data build/sft --out /kaggle/working/agentpal-lora/reports
    # the zero-shot baseline on identical items (same base model, no adapter)
    python -m training.evaluate_model --zero-shot --data build/sft --out .../reports
    # the exported GGUF through the SERVING path (llama.cpp, grammar-constrained JSON)
    python -m training.evaluate_model --gguf agentpal-qwen3-1.7b-q4_k_m.gguf ...

Backends: `hf` (transformers + PEFT adapter; greedy decoding, no grammar) or `gguf`
(agentpal LlamaCppAnalyzer: the exact serving code, JSON-schema grammar, thinking
disabled, include_rule_findings=False). The GGUF result is the one that counts for
serving; the HF result is a training-side check (its invalid-JSON rate is higher because
nothing constrains the output).

Reported, each separately: invalid-JSON rate; model-only precision/recall/F1 with Wilson
95% CIs (a unit is flagged when risk_score >= the policy review threshold, which is how
the orchestrator uses a model: score-only escalation); rules-only and rules + model
(through agentpal.orchestrator.assess) missed risky (FN), legitimate flagged (FP),
legitimate blocked, review rate, degraded rate; per category; latency p50/p95 and the
share above the 30 s serving timeout; peak memory. Bench v2 is scored by its own
score.py (rules_only, hybrid with the ML baseline, classifier_only and this model).

Labels: SFT dev = synthetic/author labels; bench v2 = the AI item author's PROPOSED
labels (dev smoke, NOT independent). Neither is a measurement of real-world accuracy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import resource
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agentpal.analyzers.base import AnalyzerInput, MalformedModelOutput, parse_model_output
from agentpal.analyzers.heuristic import HeuristicAnalyzer
from agentpal.analyzers.prompting import build_messages
from agentpal.config import load_policy
from agentpal.evaluation import binary_metrics, to_inputs, wilson_interval
from agentpal.orchestrator import assess
from agentpal.schemas import PaymentRequest

from .train_lora import EMPTY_THINK, load_records, verify_final

PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2 = PROJECT_ROOT / "benchmarks" / "agentpal-bench-v2"
SERVING_TIMEOUT_S = 30.0
MAX_NEW_TOKENS = 384
ORDER = ["allow_for_review", "require_review", "block"]


# ------------------------------------------------------------------ generators

class HFGenerator:
    """transformers backend. Prompt = chat template with enable_thinking=False, exactly
    as in training."""

    def __init__(self, base_model: str, revision: str | None, adapter: Path | None,
                 device: str | None = None):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float32 if self.device == "cpu" else (
            torch.bfloat16 if torch.cuda.get_device_capability(0)[0] >= 8 else torch.float16)
        src = str(adapter) if adapter else base_model
        self.tok = AutoTokenizer.from_pretrained(src, revision=None if adapter else revision)
        model = AutoModelForCausalLM.from_pretrained(base_model, revision=revision, dtype=dtype)
        if adapter:
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, str(adapter))
        self.model = model.to(self.device).eval()
        self.eos = self.tok.convert_tokens_to_ids("<|im_end|>")
        self.name = f"hf:{base_model}" + (f"+{adapter.name}" if adapter else " (zero-shot)")

    def __call__(self, messages: list[dict]) -> str:
        prompt = self.tok.apply_chat_template(messages, tokenize=False,
                                              add_generation_prompt=True,
                                              enable_thinking=False)
        if not prompt.endswith(EMPTY_THINK):
            raise RuntimeError("chat template did not disable thinking")
        ids = self.tok(prompt, return_tensors="pt", add_special_tokens=False).to(self.device)
        with self.torch.no_grad():
            out = self.model.generate(**ids, max_new_tokens=MAX_NEW_TOKENS, do_sample=False,
                                      eos_token_id=self.eos,
                                      pad_token_id=self.tok.pad_token_id or self.eos)
        return self.tok.decode(out[0, ids["input_ids"].shape[1]:], skip_special_tokens=True)

    def peak_memory(self) -> dict:
        if self.device == "cuda":
            return {"gpu_max_allocated_bytes": self.torch.cuda.max_memory_allocated()}
        return {}


class GenerativeEvalAnalyzer:
    """RiskAnalyzer wrapper used for evaluation: same prompt builder and parser as serving;
    records every raw output, validity and latency."""

    name = "finetuned"
    model_version = "eval"
    is_fallback = False
    reports_uncertainty = True

    def __init__(self, generate, model_name: str):
        self.generate, self.model_name = generate, model_name
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def analyze(self, data: AnalyzerInput):
        messages = build_messages(data, include_rule_findings=False)
        t0 = time.perf_counter()
        raw = self.generate(messages)
        dt = (time.perf_counter() - t0) * 1000
        rec = {"latency_ms": dt, "valid": True, "raw_chars": len(raw)}
        self.calls.append(rec)
        try:
            out = parse_model_output(raw)
        except MalformedModelOutput as exc:
            rec["valid"], rec["error"] = False, str(exc)[:200]
            raise
        rec["risk_score"] = out.risk_score
        return out


def gguf_analyzer(path: Path):
    """The serving analyzer itself (integrity checks off: an evaluation artifact)."""
    from agentpal.analyzers.llm import LlamaCppAnalyzer
    a = LlamaCppAnalyzer(path, require_integrity=False, require_read_only_storage=False,
                         include_rule_findings=False, disable_thinking=True)

    class Timed:
        name, model_version, is_fallback, reports_uncertainty = "finetuned", "gguf", False, True
        model_name = path.name

        def __init__(self):
            self.calls: list[dict] = []

        def available(self):
            return a.available()

        def analyze(self, data):
            t0 = time.perf_counter()
            rec = {"valid": True}
            try:
                out = a.analyze(data)
                rec["risk_score"] = out.risk_score
                return out
            except MalformedModelOutput as exc:
                rec["valid"], rec["error"] = False, str(exc)[:200]
                raise
            finally:
                rec["latency_ms"] = (time.perf_counter() - t0) * 1000
                self.calls.append(rec)

    return Timed()


# ------------------------------------------------------------------ metrics helpers

def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, int(round(q * len(s) + 0.5)) - 1))], 1)


def latency_summary(calls: list[dict]) -> dict:
    lat = [c["latency_ms"] for c in calls]
    return {"n": len(lat), "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95),
            "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
            "over_serving_timeout": sum(x > SERVING_TIMEOUT_S * 1000 for x in lat)}


def validity(calls: list[dict]) -> dict:
    n = len(calls)
    bad = sum(not c["valid"] for c in calls)
    return {"calls": n, "invalid": bad, "invalid_rate": round(bad / n, 4) if n else None,
            "invalid_rate_ci95": wilson_interval(bad, n),
            "examples": [c.get("error") for c in calls if not c["valid"]][:5]}


def operational(labels: list[int], actions: list[str]) -> dict:
    risky = [a for y, a in zip(labels, actions, strict=True) if y == 1]
    legit = [a for y, a in zip(labels, actions, strict=True) if y == 0]

    def rate(k, n):
        return {"count": k, "n": n, "rate": round(k / n, 4) if n else None,
                "ci95": wilson_interval(k, n)}
    flagged = [int(a != "allow_for_review") for a in actions]
    return {"missed_risky_FN": rate(sum(a == "allow_for_review" for a in risky), len(risky)),
            "legitimate_flagged_FP": rate(sum(a != "allow_for_review" for a in legit),
                                          len(legit)),
            "legitimate_blocked": rate(sum(a == "block" for a in legit), len(legit)),
            "review_rate": rate(sum(a == "require_review" for a in actions), len(actions)),
            "block_rate": rate(sum(a == "block" for a in actions), len(actions)),
            "binary": binary_metrics(labels, flagged)}


# ------------------------------------------------------------------ (a) SFT dev

def eval_sft_dev(data_dir: Path, analyzer, limit: int | None = None) -> dict:
    """Rows are the SFT dev records; the original request/context is rebuilt from the
    generator/augmentation so rules and the model see the same request."""
    from dataclasses import asdict

    from agentpal.dataset import generate

    from . import augment
    manifest = json.loads((data_dir / "manifest.json").read_text())
    recs = load_records(data_dir / "dev.jsonl")[:limit] if limit else \
        load_records(data_dir / "dev.jsonl")
    seed = manifest["seed"]
    source = {e["example_id"]: e for e in (asdict(x) for x in generate(seed=seed))}
    source |= {e["example_id"]: e for e in augment.generate(seed)}
    policy = load_policy()
    review_at = policy.recommendation.review_at_or_above
    rows = []
    for r in recs:
        req, ctx = to_inputs(_as_written(r, source[r["id"]]))
        before = len(analyzer.calls)
        a_rules = assess(req, ctx, policy, [HeuristicAnalyzer()]).recommendation
        res = assess(req, ctx, policy, [analyzer])
        call = analyzer.calls[before] if len(analyzer.calls) > before else {"valid": False}
        score = call.get("risk_score")
        rows.append({"id": r["id"], "category": r["category"], "label": r["label"],
                     "language": r.get("language", "en"), "wording": r.get("wording"),
                     "valid": call["valid"], "model_score": score,
                     "model_flag": int(score is not None and score >= review_at),
                     "rules_only": a_rules, "rules_plus_model": res.recommendation,
                     "degraded": res.degraded})
    return summarize(rows, analyzer.calls[-len(rows):] if rows else [])


def _as_written(record: dict, ex: dict) -> dict:
    """Use the request exactly as written into the SFT record (evidence toggling is part
    of the build), with the source example's context."""
    content = record["messages"][1]["content"]
    view = json.loads(content.split("<untrusted_request>")[1].split("</untrusted_request>")[0])
    req = json.loads(json.dumps(ex["request"]))
    req["evidence"] = view["evidence_claimed_by_agent"]
    for e in req["evidence"]:
        for k in [k for k, v in e.items() if v is None]:
            e.pop(k)
    return {**ex, "request": req}


def summarize(rows: list[dict], calls: list[dict]) -> dict:
    labels = [r["label"] for r in rows]
    valid = [r for r in rows if r["valid"]]
    out = {
        "units": len(rows), "risky": sum(labels), "legitimate": len(labels) - sum(labels),
        "validity": validity(calls),
        "model_only_on_valid_outputs": binary_metrics([r["label"] for r in valid],
                                                      [r["model_flag"] for r in valid]),
        "model_only_invalid_counted_as_not_flagged": binary_metrics(
            labels, [r["model_flag"] if r["valid"] else 0 for r in rows]),
        "rules_only": operational(labels, [r["rules_only"] for r in rows]),
        "rules_plus_model": operational(labels, [r["rules_plus_model"] for r in rows]),
        "rules_plus_model_degraded_rate": round(sum(r["degraded"] for r in rows) / len(rows), 4)
        if rows else None,
        "never_less_strict_than_rules": all(
            ORDER.index(r["rules_plus_model"]) >= ORDER.index(r["rules_only"]) for r in rows),
        "latency": latency_summary(calls),
        "per_category": {}, "per_language": {}, "per_wording": {},
    }
    for key, field in (("per_category", "category"), ("per_language", "language"),
                       ("per_wording", "wording")):
        for v in sorted({str(r[field]) for r in rows}):
            sel = [r for r in rows if str(r[field]) == v]
            ys = [r["label"] for r in sel]
            out[key][v] = {"n": len(sel), "risky": sum(ys),
                           "invalid": sum(not r["valid"] for r in sel),
                           "model_flagged": sum(r["model_flag"] for r in sel),
                           "rules_only_flagged": sum(r["rules_only"] != "allow_for_review"
                                                     for r in sel),
                           "rules_plus_model_flagged": sum(
                               r["rules_plus_model"] != "allow_for_review" for r in sel)}
    return out


# ------------------------------------------------------------------ (b) bench v2

def _score_module():
    spec = importlib.util.spec_from_file_location("bench_v2_score_eval", V2 / "score.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def eval_bench_v2(analyzer, out_dir: Path, name: str, max_cases: int | None = None,
                  max_episodes: int | None = None) -> dict:
    score = _score_module()
    policy = load_policy()
    review_at = policy.recommendation.review_at_or_above
    settings = score.harness_settings()
    cases = score.read_jsonl(V2 / "cases.jsonl")[:max_cases]
    episodes = score.read_jsonl(V2 / "episodes.jsonl")[:max_episodes]
    from agentpal import benchmark
    from agentpal.features import AssessmentContext
    preds, model_only = [], []
    for c in cases:
        req = PaymentRequest.model_validate(c["request"])
        ctx = AssessmentContext(now=benchmark.EPOCH, spend=benchmark._spend_for(c, req))
        before = len(analyzer.calls)
        t0 = time.perf_counter()
        res = assess(req, ctx, policy, [analyzer])
        preds.append({"unit_id": c["id"], "action": res.recommendation,
                      "latency_ms": (time.perf_counter() - t0) * 1000,
                      "degraded": res.degraded})
        call = analyzer.calls[before] if len(analyzer.calls) > before else {}
        flag = call.get("valid") and call.get("risk_score", 0) >= review_at
        model_only.append({"unit_id": c["id"], "degraded": not call.get("valid", False),
                           "action": "require_review" if flag else "allow_for_review",
                           "latency_ms": call.get("latency_ms")})
    for e in episodes:
        r = score.replay_episode(e, [analyzer], settings)
        preds += [{"unit_id": s["unit_id"], "action": s["action"] or "block",
                   "latency_ms": s["latency_ms"], "degraded": s["degraded"]}
                  for s in r["steps"]]
    out_dir.mkdir(parents=True, exist_ok=True)
    hybrid_path = out_dir / f"{name}_rules_plus_model_predictions.jsonl"
    only_path = out_dir / f"{name}_model_only_predictions.jsonl"
    score.write_jsonl(hybrid_path, preds)
    score.write_jsonl(only_path, model_only)
    units = {p["unit_id"] for p in preds}
    report = score.score(V2, "proposed", external=[f"{name}_rules_plus_model={hybrid_path}",
                                                   f"{name}_model_only={only_path}"],
                         compare_pair=("rules_only", f"{name}_rules_plus_model"),
                         only_units=units)
    report.pop("_predictions", None)
    (out_dir / f"bench_v2_{name}.json").write_text(json.dumps(report, indent=2,
                                                              ensure_ascii=False) + "\n")
    (out_dir / f"bench_v2_{name}.md").write_text(score.render_markdown(report))
    return {"units": len(units), "banner": report["banner"],
            "overall": {k: v["overall"] for k, v in report["approaches"].items()},
            "comparisons": report["comparisons"],
            "report": str(out_dir / f"bench_v2_{name}.json")}


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--adapter", help="final/ directory of a training run (HF backend)")
    g.add_argument("--zero-shot", action="store_true", help="base model, no adapter")
    g.add_argument("--gguf", help="exported GGUF, evaluated through the serving analyzer")
    p.add_argument("--base-model", default="Qwen/Qwen3-1.7B")
    p.add_argument("--revision", default=None, help="default: from COMPLETE.json / config")
    p.add_argument("--data", default="build/sft")
    p.add_argument("--out", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--limit-dev", type=int, default=None)
    p.add_argument("--max-bench-cases", type=int, default=None)
    p.add_argument("--max-bench-episodes", type=int, default=None)
    p.add_argument("--skip-bench", action="store_true")
    a = p.parse_args(argv)
    from .train_lora import TrainConfig
    meta: dict = {"evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if a.gguf:
        analyzer = gguf_analyzer(Path(a.gguf))
        meta |= {"backend": "gguf (serving path)", "model": a.gguf}
        em = Path(a.gguf).parent / "EXPORT_MANIFEST.json"
        if em.is_file():  # provenance of an exported model (run_kind, base, hashes)
            export = json.loads(em.read_text())
            meta["training_run"] = {k: export.get(k) for k in (
                "run_kind", "notice", "base_model", "base_revision", "llama_cpp_commit")}
            meta["gguf_sha256"] = export["files"].get(Path(a.gguf).name, {}).get("sha256")
        name = a.name or "gguf"
    else:
        adapter = Path(a.adapter) if a.adapter else None
        revision = a.revision
        base = a.base_model
        if adapter:
            marker = verify_final(adapter)
            meta["training_run"] = {k: marker.get(k) for k in (
                "run_kind", "notice", "base_model", "base_revision", "global_step",
                "best_eval_loss", "data_manifest_sha256", "precision")}
            base, revision = marker["base_model"], revision or marker["base_revision"]
        revision = revision or TrainConfig().base_revision
        gen = HFGenerator(base, revision, adapter)
        analyzer = GenerativeEvalAnalyzer(gen, gen.name)
        meta |= {"backend": "hf (no output grammar)", "model": gen.name,
                 "base_revision": revision}
        name = a.name or ("zero_shot" if a.zero_shot else "finetuned")
    smoke = meta.get("training_run", {}).get("run_kind") == "pipeline-smoke-test"
    meta["banner"] = ("PIPELINE SMOKE TEST - tiny random model - metrics are meaningless"
                      if smoke else "Synthetic/author labels: development evidence only, "
                                    "not real-world accuracy")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"meta": meta, "sft_dev": eval_sft_dev(Path(a.data), analyzer, a.limit_dev)}
    if not a.skip_bench:
        analyzer.calls.clear()
        report["bench_v2_dev"] = eval_bench_v2(analyzer, out, name, a.max_bench_cases,
                                               a.max_bench_episodes)
        report["bench_v2_dev"]["validity"] = validity(analyzer.calls)
        report["bench_v2_dev"]["latency"] = latency_summary(analyzer.calls)
    peak = getattr(getattr(analyzer, "generate", None), "peak_memory", lambda: {})()
    report["memory"] = {"process_max_rss_bytes":
                        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, **peak}
    report["reference_baselines"] = {
        "zero_shot_qwen2.5_1.5b_q4km_risk_model_plan": {
            "note": "docs/RISK_MODEL_PLAN.md section 1 (40 rows of the OLD SFT dev split, "
                    "without rule findings): 35/40 valid JSON, 19/19 legitimate flagged, "
                    "0/16 risky missed, p50 12.3 s / p95 16.5 s on 4 CPU cores"},
        "rules_hybrid_classifier": "see bench_v2_dev.overall (scored on identical units)"}
    path = out / f"eval_{name}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    s = report["sft_dev"]
    print(meta["banner"])
    print(f"SFT dev: n={s['units']} invalid={s['validity']['invalid_rate']} "
          f"model-only F1(valid)={s['model_only_on_valid_outputs']['f1']} "
          f"rules+model FN={s['rules_plus_model']['missed_risky_FN']['rate']} "
          f"FP={s['rules_plus_model']['legitimate_flagged_FP']['rate']} "
          f"p50/p95={s['latency']['p50_ms']}/{s['latency']['p95_ms']} ms")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
