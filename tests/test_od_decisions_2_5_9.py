"""Owner decisions 2 (hard blocks vs review), 5 (degraded mode) and 9 (model escalation).

2: only deterministic policy violations block; soft signals require review; a combination
   of signals may block only through an explicit, enabled, tested policy entry.
5: a model failure falls back to the rules, marks the assessment degraded, and requires
   review when material risk remains; it is never evidence of safety.
9: a model escalates only through its bounded score, not through its label alone.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentpal.analyzers import AnalyzerUnavailable, HeuristicAnalyzer
from agentpal.config import CombinationBlock, load_policy
from agentpal.orchestrator import assess, recommendation_for
from agentpal.schemas import ModelFinding

from conftest import FakeAnalyzer, analysis, ctx, make_request

# Lookalike of an approved merchant, using that merchant's name: only soft signals.
IMPERSONATION = dict(merchant_domain="examp1e.com", amount=49)


def with_combo(policy, enabled=True):
    p = policy.model_copy(deep=True)
    p.recommendation.combination_blocks = [CombinationBlock(
        name="impersonation_lookalike", enabled=enabled,
        all_of=["LOOKALIKE_DOMAIN", "MERCHANT_DOMAIN_CHANGED"],
        rationale="Domain imitates an approved merchant and uses that merchant's name.")]
    return p


# ------------------------------------------------------------ decision 2

def test_soft_signals_alone_never_block(policy):
    a = assess(make_request(**IMPERSONATION), ctx(), policy, [HeuristicAnalyzer()])
    assert a.rule_score >= 75 and a.risk_category == "critical"
    assert not a.hard_violations
    assert a.recommendation == "require_review"
    assert "human review" in a.explanation


def test_score_alone_maps_to_review_not_block(policy):
    assert recommendation_for(100, policy) == "require_review"
    assert recommendation_for(100, policy, hard_violation=True) == "block"


def test_hard_policy_violation_still_blocks(policy):
    a = assess(make_request(amount=150), ctx(), policy, [HeuristicAnalyzer()])
    assert "BUDGET_EXCEEDED" in a.hard_violations and a.recommendation == "block"


def test_shipped_combination_policy_is_disabled(policy):
    combos = policy.recommendation.combination_blocks
    assert combos and all(not c.enabled for c in combos)


def test_enabled_combination_blocks_and_is_explained(policy):
    a = assess(make_request(**IMPERSONATION), ctx(), with_combo(policy), [HeuristicAnalyzer()])
    assert a.recommendation == "block"
    assert "COMBINATION_BLOCK:impersonation_lookalike" in a.hard_violations
    sig = next(s for s in a.signals if s.code == "COMBINATION_BLOCK:impersonation_lookalike")
    assert sig.evidence["all_of"] == ["LOOKALIKE_DOMAIN", "MERCHANT_DOMAIN_CHANGED"]
    assert "imitates an approved merchant" in sig.evidence["rationale"]
    assert "impersonation_lookalike" in a.explanation


def test_disabled_combination_does_not_block(policy):
    a = assess(make_request(**IMPERSONATION), ctx(), with_combo(policy, enabled=False),
               [HeuristicAnalyzer()])
    assert a.recommendation == "require_review"


def test_model_findings_cannot_satisfy_a_combination(policy):
    findings = [ModelFinding(code="LOOKALIKE_DOMAIN", description="x"),
                ModelFinding(code="MERCHANT_DOMAIN_CHANGED", description="y")]
    a = assess(make_request(), ctx(), with_combo(policy),
               [FakeAnalyzer(analysis(score=100, rec="block", findings=findings))])
    assert a.recommendation != "block" and not a.hard_violations


GOOD_COMBO = {"name": "combo_ok", "all_of": ["LOOKALIKE_DOMAIN", "MERCHANT_DOMAIN_CHANGED"],
              "enabled": True, "rationale": "Lookalike of an approved merchant's domain."}


def _with_combo_dict(combo):
    p = load_policy().model_dump()
    p["recommendation"]["combination_blocks"] = [combo]
    return type(load_policy()).model_validate(p)


def test_valid_combination_config_is_accepted():
    assert _with_combo_dict(GOOD_COMBO).recommendation.combination_blocks[0].enabled


@pytest.mark.parametrize("defect", [
    {"all_of": ["LOOKALIKE_DOMAIN", "NOT_A_RULE"]},   # unknown rule code
    {"all_of": ["LOOKALIKE_DOMAIN"]},                 # a single signal is not a combination
    {"name": "x"},                                    # name pattern
    {"rationale": "r"},                               # must be explainable
])
def test_invalid_combination_config_is_rejected(defect):
    with pytest.raises(ValidationError):
        _with_combo_dict({**GOOD_COMBO, **defect})


# ------------------------------------------------------------ decision 9 (escalation)

def test_model_label_alone_does_not_escalate(policy):
    rules_only = assess(make_request(), ctx(), policy, [HeuristicAnalyzer()])
    a = assess(make_request(), ctx(), policy,
               [FakeAnalyzer(analysis(score=rules_only.rule_score, rec="require_review",
                                      uncertainty=0.1))])
    assert a.recommendation == rules_only.recommendation


def test_model_escalates_through_bounded_score(policy):
    a = assess(make_request(), ctx(), policy,
               [FakeAnalyzer(analysis(score=90, rec="require_review", uncertainty=0.1))])
    assert a.risk_score >= policy.recommendation.review_at_or_above
    assert a.recommendation == "require_review"


def test_label_escalation_can_be_enabled_explicitly(policy):
    p = policy.model_copy(deep=True)
    p.recommendation.model_escalation = "label_or_score"
    a = assess(make_request(), ctx(), p,
               [FakeAnalyzer(analysis(score=0, rec="require_review", uncertainty=0.1))])
    assert a.recommendation == "require_review"


# ------------------------------------------------------------ decision 5 (degraded)

@pytest.mark.parametrize("backend", [
    FakeAnalyzer(available=False),
    FakeAnalyzer(exc=AnalyzerUnavailable("down")),
    FakeAnalyzer(exc=RuntimeError("bug")),
    FakeAnalyzer(raw="not json"),
])
def test_model_failure_marks_degraded_and_requires_review(policy, backend):
    a = assess(make_request(), ctx(), policy, [backend, HeuristicAnalyzer()])
    assert a.degraded and a.degraded_reason
    assert a.model.backend == "heuristic"
    assert "MODEL_UNAVAILABLE_OR_TIMED_OUT" in {s.code for s in a.signals}
    assert a.rule_score > 0  # material unresolved risk
    assert a.recommendation == "require_review"
    assert "degraded" in a.explanation.lower()


def test_degraded_never_less_strict_than_rules_only(policy):
    for req in (make_request(), make_request(amount=150), make_request(**IMPERSONATION)):
        rules_only = assess(req, ctx(), policy, [HeuristicAnalyzer()])
        degraded = assess(req, ctx(), policy, [FakeAnalyzer(available=False)])
        order = {"allow_for_review": 0, "require_review": 1, "block": 2}
        assert order[degraded.recommendation] >= order[rules_only.recommendation]


def test_degraded_review_threshold_is_configurable(policy):
    p = policy.model_copy(deep=True)
    p.recommendation.degraded_mode.review_if_rule_score_at_or_above = 101
    a = assess(make_request(), ctx(), p, [FakeAnalyzer(available=False)])
    assert a.degraded  # still recorded, even when it does not force review


def test_rules_only_configuration_is_not_degraded(policy):
    a = assess(make_request(), ctx(), policy, [HeuristicAnalyzer()])
    assert not a.degraded and a.degraded_reason is None


def test_fallback_to_a_second_model_is_not_degraded(policy):
    a = assess(make_request(), ctx(), policy,
               [FakeAnalyzer(exc=RuntimeError("bug")), FakeAnalyzer(analysis(score=5))])
    assert a.model.backend == "fake" and a.model.errors
    assert not a.degraded and a.degraded_reason is None


def _zero_point_policy(policy, threshold):
    p = policy.model_copy(deep=True)
    p.rule_points["MISSING_EVIDENCE"] = 0
    p.recommendation.degraded_mode.review_if_rule_score_at_or_above = threshold
    return p


def test_degraded_with_zero_rule_points_is_never_presented_as_safe(policy):
    """Final owner decision 1: zero rule points in a degraded assessment does not mean the
    payment is safe. It is marked degraded, carries an uncertainty signal, and its
    explanation says so; allow_for_review never authorizes a payment."""
    a = assess(make_request(), ctx(), _zero_point_policy(policy, 1),
               [FakeAnalyzer(available=False)])
    assert a.rule_score == 0 and a.degraded
    assert "MODEL_UNAVAILABLE_OR_TIMED_OUT" in {s.code for s in a.signals}
    assert "not evidence of safety" in a.explanation
    assert "does not authorize" in a.explanation
    assert a.payment_executed is False


def test_threshold_zero_makes_every_degraded_assessment_require_review(policy):
    a = assess(make_request(), ctx(), _zero_point_policy(policy, 0),
               [FakeAnalyzer(available=False)])
    assert a.rule_score == 0 and a.recommendation == "require_review"


def test_shipped_degraded_policy_is_the_conservative_one(policy):
    assert policy.recommendation.degraded_mode.review_if_rule_score_at_or_above == 1
