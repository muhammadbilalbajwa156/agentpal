"""ML-11: a locked run must score ONLY the locked adjudicated labels. Before the fix the
scorer checked `Path(labels).name` at the top of the bench dir, so a labels file in a
subdirectory (never locked, never hashed) was accepted for a locked run."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from agentpal import benchmark
from agentpal.config import PACKAGE_ROOT

V2 = PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v2"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("bench_v2_score_locked", V2 / "score.py")
custodian = _load("bench_v2_custodian_locked", V2 / "custodian_lock.py")
CASES, _ = benchmark.load(V2)


@pytest.fixture
def locked(tmp_path):
    d = tmp_path / "bench"
    d.mkdir()
    cases = [{**c, "independent": True, "split": "locked"} for c in CASES[:4]]
    (d / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
    rows = [{"item_id": c["id"], "risky": c["proposed_label"],
             "expected_action": c["proposed_expected_action"], "n_annotators": 3,
             "method": "majority", "annotators": ["a0", "a1", "a2"]} for c in cases]
    (d / "adjudicated_labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    custodian.lock(d, "test")
    return d, rows


def _args(d, labels, out):
    return ["--split", "locked", "--bench-dir", str(d), "--labels", str(labels),
            "--approaches", "rules_only", "--out-dir", str(out)]


def test_locked_run_with_the_locked_labels_still_works(locked, tmp_path):
    d, _ = locked
    assert score.main(_args(d, d / "adjudicated_labels.jsonl", tmp_path / "r")) == 0


@pytest.mark.parametrize("where", ["sub/adjudicated_labels.jsonl", "sub/other.jsonl",
                                   "other_labels.jsonl"])
def test_unlocked_labels_are_refused_for_a_locked_run(locked, tmp_path, where):
    d, rows = locked
    forged = d / where
    forged.parent.mkdir(exist_ok=True)
    flipped = [{**r, "risky": 1 - r["risky"],
                "expected_action": "allow_for_review" if r["risky"] else "require_review"}
               for r in rows]
    forged.write_text("".join(json.dumps(r) + "\n" for r in flipped))
    assert score.main(_args(d, forged, tmp_path / "r")) == 2
    assert not (d / "access_log.jsonl").exists()      # nothing was scored or logged
