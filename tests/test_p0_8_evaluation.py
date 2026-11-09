"""P0.8: evaluation honesty (CIs, retired split) and the AgentPal-Bench harness."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentpal import benchmark
from agentpal.config import PACKAGE_ROOT
from agentpal.dataset import write_dataset
from agentpal.evaluation import binary_metrics, evaluate, fit_baseline, wilson_interval

from conftest import VALID

SEED = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v1"


def test_metrics_report_wilson_intervals():
    m = binary_metrics([1] * 48 + [0] * 12, [1] * 60)
    assert m["false_positive_rate"] == 1.0
    assert m["ci95"]["false_positive_rate"] == pytest.approx([0.7575, 1.0], abs=1e-3)
    assert wilson_interval(0, 0) is None


def test_old_test_split_is_marked_retired(tmp_path):
    write_dataset(tmp_path / "d", seed=5)
    fit_baseline(tmp_path / "d", tmp_path / "m.json")
    r = evaluate(tmp_path / "d", tmp_path / "m.json", tmp_path / "r.json", split="test")
    assert r["split_status"].startswith("retired_for_model_selection")
    v = evaluate(tmp_path / "d", tmp_path / "m.json", tmp_path / "v.json", split="validation")
    assert v["split_status"] == "in_use"
    assert "ml_artifact_sha256" in r  # provenance of the evaluated artifact


def _bench(tmp_path, cases, episodes=()) -> Path:
    d = tmp_path / "bench"
    d.mkdir()
    (d / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
    (d / "episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
    return d


def case(i, group, split="dev", label=0, **req):
    return {"id": f"c{i}", "group_id": group, "split": split, "label": label, "author": "t",
            "independent": True, "rationale": "t",
            "request": {**VALID, "request_id": f"req_c{i}", **req}}


def test_groups_may_not_cross_splits(tmp_path):
    d = _bench(tmp_path, [case(1, "g1", "dev"), case(2, "g1", "locked")])
    with pytest.raises(benchmark.BenchmarkError, match="both"):
        benchmark.load(d)


def test_locked_split_requires_lock_and_detects_tampering(tmp_path):
    d = _bench(tmp_path, [case(1, "g1", "locked")])
    with pytest.raises(benchmark.BenchmarkError, match="LOCK.json"):
        benchmark.evaluate_benchmark(d, "locked", tmp_path / "r.json")
    benchmark.lock(d, note="test")
    benchmark.evaluate_benchmark(d, "locked", tmp_path / "r.json")
    assert len((d / "access_log.jsonl").read_text().splitlines()) == 1
    with (d / "cases.jsonl").open("a") as fh:
        fh.write(json.dumps(case(2, "g2", "locked")) + "\n")
    with pytest.raises(benchmark.BenchmarkError, match="changed after"):
        benchmark.evaluate_benchmark(d, "locked", tmp_path / "r2.json")


def test_all_approaches_scored_on_identical_cases(tmp_path):
    d = _bench(tmp_path, [case(1, "g1"), case(2, "g2", label=1, purpose="Gift cards for team",
                                              recurring=False, billing_interval=None)])
    r = benchmark.evaluate_benchmark(d, "dev", tmp_path / "r.json")
    rows = r["cases"]["predictions"]
    assert [x["id"] for x in rows] == ["c1", "c2"]
    assert set(r["cases"]["metrics"]) <= {"rules_only", "classifier_only", "hybrid"}
    for row in rows:
        assert set(r["cases"]["metrics"]) <= set(row)


def test_episodes_replayed_through_api(tmp_path):
    episode = {"id": "e1", "group_id": "ge1", "split": "dev", "author": "t", "independent": True,
               "steps": [{"request": {**VALID, "request_id": "req_e1"}, "expect": {}},
                         {"advance_seconds": 5, "request": {**VALID, "request_id": "req_e1"},
                          "expect": {"action": "block", "codes": ["DUPLICATE_REQUEST_ID"]}}]}
    d = _bench(tmp_path, [], [episode])
    r = benchmark.evaluate_benchmark(d, "dev", tmp_path / "r.json")
    rules = r["episodes"]["rules_only"]
    assert rules["steps_passed"] == rules["steps"] == 2


def test_seed_benchmark_is_valid_and_flagged_not_independent(tmp_path):
    cases, episodes = benchmark.load(SEED)
    assert cases and episodes
    assert all(not i["independent"] for i in cases + episodes)
    assert all(i["split"] == "dev" for i in cases + episodes)  # no locked split yet
    d = tmp_path / "seed"
    shutil.copytree(SEED, d)
    r = benchmark.evaluate_benchmark(d, "dev", tmp_path / "seed.json")
    assert r["independent_items"] == 0 and r["items"] == len(cases) + len(episodes)


def test_operational_metrics_report_misses_false_positives_and_rates_separately():
    from agentpal.evaluation import operational_metrics

    o = operational_metrics([1, 1, 1, 0, 0], ["allow_for_review", "require_review", "block",
                                              "require_review", "allow_for_review"],
                            [False, True, False, False, False])
    assert o["risky_missed"] == 1 and o["risky_miss_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert o["legitimate_flagged"] == 1 and o["legitimate_flag_rate"] == 0.5
    assert (o["allow_rate"], o["review_rate"], o["block_rate"]) == (0.4, 0.4, 0.2)
    assert o["degraded_rate"] == 0.2 and o["legitimate_blocked"] == 0


def test_benchmark_harness_runs_outside_the_test_dev_auth_fixture(tmp_path, monkeypatch):
    """Integration (SEC-01 merge): the library entry point must not depend on the
    session-wide dev-auth environment that pytest sets."""
    monkeypatch.delenv("AGENTPAL_AUTH_MODE", raising=False)
    d = tmp_path / "seed"
    shutil.copytree(SEED, d)
    r = benchmark.evaluate_benchmark(d, "dev", tmp_path / "seed.json")
    rules = r["episodes"]["rules_only"]
    assert rules["steps_passed"] == rules["steps"]


def test_http_refusal_is_a_failed_step_not_a_crash(tmp_path):
    from agentpal.config import Settings

    ep = {"id": "e", "group_id": "g", "split": "dev", "author": "t", "independent": True,
          "steps": [{"request": {**VALID, "request_id": "req_refused"},
                     "expect": {}, "then": [{"op": "owner_decision", "decision": "approved"}]}]}
    res = benchmark.replay_episode(ep, [], Settings(db_path=":memory:", auth_mode="required"))
    assert res == [{"step": 0, "passed": False, "recommendation": None, "codes": [],
                    "expected": {}, "http_status": 401, "known_gap": ""}]
