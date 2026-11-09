"""Final owner decision 4: the USD 100 / GBP 80 / JPY 15000 default budgets are demo
placeholders. They are accepted only in a configuration explicitly labelled "demo", and an
agent that falls back to them always requires review. Outside a demo configuration an
unlisted agent is rejected (NO_SPENDING_POLICY). Agents can never set their own budgets."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import PACKAGE_ROOT, load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy, load_spend_policy
from agentpal.store import AssessmentStore

from conftest import VALID

LIMITS = {"USD": {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


def policy(configuration, default=True):
    return SpendPolicy.model_validate({
        "policy_version": "t", "configuration": configuration,
        "default_agent": {"owner_id": "o", "currencies": LIMITS} if default else None,
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": LIMITS}}})


def assess(sp, agent, rid="req_db_1", **kw):
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                    spend_policy=sp)
    return s.assess(PaymentRequest.model_validate({**VALID, "request_id": rid,
                                                   "agent_id": agent, **kw}))


def codes(a):
    return {s.code for s in a.signals}


def test_operator_configuration_cannot_hold_default_budgets():
    with pytest.raises(ValidationError, match="demo"):
        policy("operator")
    assert SpendPolicy.model_validate({"policy_version": "t"}).configuration == "operator"
    with pytest.raises(ValidationError):  # the default is operator, so this is refused too
        SpendPolicy.model_validate({"policy_version": "t", "default_agent": {
            "owner_id": "o", "currencies": LIMITS}})


def test_unlisted_agent_outside_demo_is_rejected():
    a = assess(policy("operator", default=False), "agent_unlisted_9")
    assert "NO_SPENDING_POLICY" in a.hard_violations and a.recommendation == "block"


def test_demo_default_budget_is_labelled_and_always_requires_review():
    a = assess(policy("demo"), "agent_unlisted_9")
    assert "DEMO_DEFAULT_BUDGET_APPLIED" in codes(a)
    assert a.recommendation == "require_review"
    listed = assess(policy("demo"), "agent_demo_001", rid="req_db_2")
    assert "DEMO_DEFAULT_BUDGET_APPLIED" not in codes(listed)


def test_agent_cannot_raise_its_own_budget():
    a = assess(policy("operator", default=False), "agent_demo_001", amount="150.00",
               monthly_budget=1_000_000, budget_spent_this_period=0)
    assert "BUDGET_EXCEEDED" in a.hard_violations


def test_shipped_files_are_labelled():
    shipped = load_spend_policy()
    assert shipped.configuration == "demo" and shipped.default_agent is not None
    assert "DEMO" in shipped.note and "placeholder" in shipped.note
    example = json.loads((PACKAGE_ROOT / "config" / "spend_policy.operator.example.json")
                         .read_text())
    op = SpendPolicy.model_validate(example)
    assert op.configuration == "operator" and op.default_agent is None
