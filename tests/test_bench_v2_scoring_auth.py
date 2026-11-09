"""The bench v2 scorer replays episodes in-process; it must not depend on the process's
AGENTPAL_AUTH_MODE, and an HTTP refusal must be a failed unit, never a crash."""

from __future__ import annotations

import importlib.util
import sys

import httpx
from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import PACKAGE_ROOT

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("bench_v2_score_auth", V2 / "score.py")


def _cases_and_two_episodes() -> set[str]:
    units = score.bench_units(V2)
    first = sorted({u["item_id"] for u in units if u["kind"] == "episode_step"})[:2]
    return {u["unit_id"] for u in units if u["kind"] == "case" or u["item_id"] in first}


def test_harness_settings_use_the_explicit_dev_auth_mode_when_available(monkeypatch):
    monkeypatch.delenv("AGENTPAL_AUTH_MODE", raising=False)
    s = score.harness_settings()
    if "auth_mode" in type(s).model_fields:
        assert s.auth_mode == "disabled_insecure_dev"
    assert s.db_path == ":memory:"


def test_scorer_runs_with_auth_mode_unset(monkeypatch):
    monkeypatch.delenv("AGENTPAL_AUTH_MODE", raising=False)
    r = score.score(V2, "proposed", approaches=("rules_only",),
                    only_units=_cases_and_two_episodes())
    m = r["approaches"]["rules_only"]
    assert m["episode_steps"]["units"] > 0 and m["single_cases"]["units"] > 0
    assert m["overall"]["http_rejected"] == 0
    ops = r["ledger_operation_checks"]["rules_only"]
    assert not [o for o in ops["mismatches"] if o["actual"] == "not_run"]


def test_http_refusal_is_a_failed_unit_not_a_crash(monkeypatch):
    real_post = TestClient.post

    def refuse(self, url, *a, **kw):
        if url == "/assess":
            return httpx.Response(401, json={"detail": "authentication required"})
        return real_post(self, url, *a, **kw)

    monkeypatch.setattr(TestClient, "post", refuse)
    episode = next(e for e in score.read_jsonl(V2 / "episodes.jsonl")
                   if any(s.get("then") for s in e["steps"]))
    out = score.replay_episode(episode, [HeuristicAnalyzer()], score.harness_settings())
    assert all(s["http_status"] == 401 and s["action"] is None for s in out["steps"])
    assert out["ops"] and all(o["actual"] == "not_run" for o in out["ops"])
    units = [{"unit_id": s["unit_id"], "kind": "episode_step"} for s in out["steps"]]
    labels = {u["unit_id"]: {"risky": 0, "expected_action": "allow_for_review"}
              for u in units}
    m = score.metrics(units, labels, {s["unit_id"]: s for s in out["steps"]})
    assert m["http_rejected"] == len(units)                 # reported, not hidden
    assert m["legitimate_blocked"]["count"] == len(units)   # a refusal stops the payment
    checks = score.op_checks(out["ops"])
    assert checks["as_expected"] < checks["checked"] or checks["checked"] == 0
