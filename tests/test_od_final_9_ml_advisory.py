"""Final owner decision 9: deterministic rules are the security authority. The ML baseline
is advisory: it may add a review, but it never blocks, never removes a block and never
relaxes the rules-only recommendation. Checked with the real pinned artifact on every
shipped example and seed-benchmark case (identical inputs for both chains)."""

from __future__ import annotations

import json

import pytest

from agentpal import benchmark
from agentpal.analyzers import HeuristicAnalyzer, MLAnalyzer
from agentpal.config import PACKAGE_ROOT, load_policy
from agentpal.features import AssessmentContext
from agentpal.integrity import load_manifest
from agentpal.orchestrator import assess
from agentpal.schemas import PaymentRequest

from conftest import NOW, fixture_spend

ORDER = {"allow_for_review": 0, "require_review": 1, "block": 2}


def _requests():
    out = []
    for f in sorted((PACKAGE_ROOT / "examples" / "requests").glob("*.json")):
        try:
            out.append(PaymentRequest.model_validate(json.loads(f.read_text())))
        except ValueError:
            pass  # the deliberately invalid example
    cases, _ = benchmark.load(PACKAGE_ROOT / "benchmarks" / "agentpal-bench-v1")
    out += [PaymentRequest.model_validate(c["request"]) for c in cases]
    return out


@pytest.fixture(scope="module")
def ml():
    m = MLAnalyzer(PACKAGE_ROOT / "artifacts" / "baseline_classifier.json",
                   load_manifest(PACKAGE_ROOT / "config" / "model_manifest.json")
                   .digest_for("ml"))
    if not m.available():
        pytest.skip("pinned ML artifact not available")
    return m


@pytest.mark.parametrize("req", _requests(), ids=lambda r: r.request_id)
def test_ml_is_advisory_only(req, ml):
    policy = load_policy()
    ctx = AssessmentContext(now=NOW, spend=fixture_spend(req))
    rules = assess(req, ctx, policy, [HeuristicAnalyzer()])
    hybrid = assess(req, ctx, policy, [ml, HeuristicAnalyzer()])
    assert hybrid.hard_violations == rules.hard_violations
    assert (hybrid.recommendation == "block") == (rules.recommendation == "block")
    assert ORDER[hybrid.recommendation] >= ORDER[rules.recommendation]
