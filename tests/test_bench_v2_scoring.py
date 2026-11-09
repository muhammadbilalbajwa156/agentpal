"""AgentPal-Bench v2 scoring: statistics, the independence gate, identity with the v1
harness, locked-split handling and the custodian lock.

The smoke run here uses the item author's PROPOSED labels; it checks that scoring works,
not how good AgentPal is."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from agentpal import benchmark
from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import PACKAGE_ROOT, Settings
from agentpal.evaluation import wilson_interval

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("bench_v2_score", V2 / "score.py")
custodian = _load("bench_v2_custodian_lock", V2 / "custodian_lock.py")
CASES, EPISODES = benchmark.load(V2)


@pytest.fixture(scope="module")
def smoke():
    return score.score(V2, "proposed", approaches=("rules_only",))


# ------------------------------------------------------------------ statistics

@pytest.mark.parametrize("k,n", [(0, 10), (3, 10), (10, 10), (41, 137), (1, 1)])
def test_wilson_matches_the_project_implementation(k, n):
    assert score.wilson(k, n) == wilson_interval(k, n)


def test_mcnemar_exact_and_interval():
    a = [True] * 96 + [False] * 12 + [False] * 29
    b = [True] * 96 + [True] * 12 + [False] * 29
    m = score.mcnemar(a, b)
    assert m["only_b_correct"] == 12 and m["only_a_correct"] == 0
    assert m["p_value_exact"] == pytest.approx(2 / 2 ** 12, abs=1e-6)
    assert m["difference_ci95_agresti_min"][0] > 0
    assert m["significant_at_0_05_and_ci_excludes_0"]
    same = score.mcnemar([True, False] * 5, [True, False] * 5)
    assert same["p_value_exact"] == 1.0 and not same["significant_at_0_05_and_ci_excludes_0"]
    small = score.mcnemar([False] * 3 + [True] * 7, [True] * 3 + [True] * 7)
    assert small["p_value_exact"] == pytest.approx(0.25)       # 3 discordant: not significant
    assert not small["significant_at_0_05_and_ci_excludes_0"]


def test_percentile_is_nearest_rank():
    v = list(range(1, 101))
    assert score.percentile(v, 0.5) == 50 and score.percentile(v, 0.95) == 95
    assert score.percentile([], 0.5) is None


# ------------------------------------------------------------------ smoke run

def test_smoke_run_scores_every_unit_and_is_labelled_self_labelled(smoke):
    assert smoke["counts"]["units_scored"] == len(CASES) + sum(len(e["steps"])
                                                               for e in EPISODES)
    assert "DEV SMOKE RUN" in smoke["banner"] and "SELF-LABELLED" in smoke["banner"]
    assert smoke["independence"]["independent"] is False
    assert smoke["independence"]["status"] == "NOT INDEPENDENT"
    o = smoke["approaches"]["rules_only"]["overall"]
    assert o["http_rejected"] == 0                      # every item is a valid request
    for key in ("missed_risky_FN", "legitimate_flagged_FP", "legitimate_blocked",
                "required_block_missed", "review_rate", "block_rate"):
        assert o[key]["ci95"] is not None, key
    assert o["latency_ms"]["p50"] is not None and o["latency_ms"]["p95"] is not None
    per = smoke["approaches"]["rules_only"]
    assert {"plain", "paraphrased", "non_english", "obfuscated"} <= set(per["per_wording"])
    assert "injection" in per["per_risk_type"]


def test_ledger_operations_follow_the_owner_policy(smoke):
    """Approval binding, the 15-minute hold, attempted-not-completed and unverified outcomes
    (F5, decisions 4 and 6): every simulated ledger event is accepted or refused as the
    policy requires."""
    ops = smoke["ledger_operation_checks"]["rules_only"]
    assert ops["checked"] >= 40 and ops["mismatches"] == []


def test_single_case_predictions_match_the_v1_harness():
    labelled = [{**c, "label": c["proposed_label"]} for c in CASES]
    stock = benchmark.run_cases(labelled, Settings(analyzer_chain=["heuristic"],
                                                   db_path=":memory:"))
    ours = score.predict_cases(CASES, ["rules_only"])["rules_only"]
    for row in stock["predictions"]:
        assert ours[row["id"]]["action"] == row["rules_only"], row["id"]


@pytest.mark.parametrize("episode", [e for e in EPISODES if e["harness_v1_compatible"]],
                         ids=lambda e: e["id"])
def test_compatible_episodes_replay_identically_in_the_v1_harness(episode):
    settings = Settings(db_path=":memory:")
    stock = benchmark.replay_episode(episode, [HeuristicAnalyzer()], settings)
    ours = score.replay_episode(episode, [HeuristicAnalyzer()], settings)
    assert [s["recommendation"] for s in stock] == [s["action"] for s in ours["steps"]]
    assert all(o["actual"] == "accepted" for o in ours["ops"])


def test_extended_episodes_record_refusals_instead_of_crashing():
    ep = next(e for e in EPISODES if "attempt-with-other-terms" in e["id"])
    res = score.replay_episode(ep, [HeuristicAnalyzer()], Settings(db_path=":memory:"))
    assert [o["actual"] for o in res["ops"]] == ["accepted", "refused", "refused", "refused",
                                                 "accepted"]
    assert {o["error"] for o in res["ops"] if o["error"]} == {"ApprovalTermsMismatch"}


def test_replay_rejects_a_backwards_clock():
    ep = json.loads(json.dumps(EPISODES[0]))
    ep["steps"][1]["advance_seconds"] = -60
    with pytest.raises(ValueError, match="backwards"):
        score.replay_episode(ep, [HeuristicAnalyzer()], Settings(db_path=":memory:"))


def test_markdown_rendering_carries_the_banner_and_caveats(smoke):
    md = score.render_markdown({k: v for k, v in smoke.items() if k != "_predictions"})
    assert "DEV SMOKE RUN" in md and "NOT INDEPENDENT" in md
    assert "Mandatory caveats" in md and "{{" not in md


def test_external_predictions_are_scored_alongside(tmp_path):
    rows = [{"unit_id": c["id"], "action": "require_review"} for c in CASES[:20]]
    f = tmp_path / "p.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = score.score(V2, "proposed", approaches=(), external=[f"always_review={f}"])
    m = rep["approaches"]["always_review"]["overall"]
    assert m["units"] == 20 and m["missed_risky_FN"]["count"] == 0


def test_committed_smoke_report_is_marked_dev_self_labelled():
    rep = json.loads((V2 / "reports" / "dev_smoke_self_labelled.json").read_text("utf-8"))
    assert rep["label_source"] == "proposed" and rep["split"] == "dev"
    assert rep["independence"]["independent"] is False
    assert "DEV SMOKE RUN" in rep["banner"] and "SELF-LABELLED" in rep["banner"]
    md = (V2 / "reports" / "dev_smoke_self_labelled.md").read_text("utf-8")
    assert "DEV SMOKE RUN" in md


# ------------------------------------------------------------------ independence gate

def _mini_bench(tmp_path, independent: bool, n_annotators: int, split: str = "dev",
                n_cases: int = 6):
    d = tmp_path / "bench"
    d.mkdir()
    cases = [{**c, "independent": independent, "split": split} for c in CASES[:n_cases]]
    (d / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
    labels = [{"item_id": c["id"], "risky": c["proposed_label"],
               "expected_action": c["proposed_expected_action"],
               "n_annotators": n_annotators, "method": "majority",
               "annotators": [f"a{i}" for i in range(n_annotators)]} for c in cases]
    (d / "adjudicated_labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in labels))
    return d


@pytest.mark.parametrize("independent,n,expected", [
    (True, 3, True), (True, 2, False), (False, 3, False)])
def test_independence_requires_independent_items_and_three_annotators(
        tmp_path, independent, n, expected):
    d = _mini_bench(tmp_path, independent, n)
    rep = score.score(d, str(d / "adjudicated_labels.jsonl"), approaches=("rules_only",))
    assert rep["independence"]["independent"] is expected
    assert ("NOT INDEPENDENT" in rep["banner"]) is (not expected)


def test_require_independent_flag_refuses_self_labelled_results(tmp_path):
    rc = score.main(["--labels", "proposed", "--approaches", "rules_only",
                     "--out-dir", str(tmp_path), "--require-independent"])
    assert rc == 2


def test_dev_run_refuses_a_directory_that_contains_locked_items(tmp_path):
    d = _mini_bench(tmp_path, True, 3, split="locked")
    with pytest.raises(SystemExit, match="not in split 'dev'"):
        score.score(d, str(d / "adjudicated_labels.jsonl"), approaches=("rules_only",))


# ------------------------------------------------------------------ locked workflow

def test_locked_scoring_refuses_a_directory_inside_the_repository(tmp_path):
    rc = score.main(["--split", "locked", "--bench-dir", str(V2), "--labels",
                     str(V2 / "x.jsonl"), "--out-dir", str(tmp_path)])
    assert rc == 2


def test_custodian_lock_refuses_inside_repo_and_without_labels(tmp_path):
    with pytest.raises(SystemExit, match="inside the repository"):
        custodian.lock(V2, "x")
    d = tmp_path / "locked"
    d.mkdir()
    (d / "cases.jsonl").write_text(json.dumps({**CASES[0], "split": "locked",
                                               "independent": True}) + "\n")
    with pytest.raises(SystemExit, match="adjudicated_labels"):
        custodian.lock(d, "x")


def test_custodian_lock_refuses_dev_items(tmp_path):
    d = _mini_bench(tmp_path, True, 3, split="dev")
    with pytest.raises(SystemExit, match="split=locked"):
        custodian.lock(d, "x")


def test_locked_end_to_end_aggregate_only_logged_and_retired(tmp_path):
    d = _mini_bench(tmp_path, True, 3, split="locked", n_cases=8)
    rec = custodian.lock(d, "test lock")
    assert set(rec["files"]) == {"cases.jsonl", "adjudicated_labels.jsonl"}
    benchmark._verify_lock(d)                         # compatible with the v1 verifier
    with pytest.raises(SystemExit, match="relock"):
        custodian.lock(d, "again")
    out = tmp_path / "reports"
    args = ["--split", "locked", "--bench-dir", str(d), "--labels",
            str(d / "adjudicated_labels.jsonl"), "--approaches", "rules_only",
            "--out-dir", str(out), "--name", "locked_run"]
    assert score.main(args) == 0
    text = (out / "locked_run.json").read_text("utf-8")
    report = json.loads(text)
    assert report["independence"]["independent"] is True
    assert "_predictions" not in report
    for c in CASES[:8]:                               # no item ids or request text leak
        assert c["id"] not in text and c["request"]["purpose"] not in text
    log = [json.loads(x) for x in (d / "access_log.jsonl").read_text().splitlines()]
    assert len(log) == 1 and log[0]["rules_version"] and "commit" in log[0]
    assert (d / "custodian_outputs" / "locked_run_predictions.jsonl").exists()
    # tampering with the labels after the lock is detected
    labels = d / "adjudicated_labels.jsonl"
    original = labels.read_text()
    labels.write_text(original.replace('"method": "majority"', '"method": "adjudicator"', 1))
    with pytest.raises(benchmark.BenchmarkError):
        score.main(args)
    labels.write_text(original)
    for _ in range(4):
        assert score.main(args) == 0
    assert score.main(args) == 2                      # retired after five locked runs


def test_locked_scoring_refuses_files_added_after_the_lock(tmp_path):
    d = _mini_bench(tmp_path, True, 3, split="locked")
    custodian.lock(d, "lock without episodes")
    ep = {**EPISODES[0], "split": "locked", "independent": True}
    (d / "episodes.jsonl").write_text(json.dumps(ep) + "\n")
    rc = score.main(["--split", "locked", "--bench-dir", str(d), "--labels",
                     str(d / "adjudicated_labels.jsonl"), "--approaches", "rules_only",
                     "--out-dir", str(tmp_path / "r")])
    assert rc == 2
