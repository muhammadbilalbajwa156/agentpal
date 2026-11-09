from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from agentpal.analyzers import AnalyzerUnavailable, HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.features import AssessmentContext
from agentpal.schemas import ModelAnalysis, PaymentRequest
from agentpal.spending import ApprovalTerms, SpendSnapshot

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

VALID = {
    "request_id": "req_demo_001",
    "agent_id": "agent_demo_001",
    "amount": 49.00,
    "currency": "USD",
    "merchant_name": "Example Hosting",
    "merchant_domain": "example.com",
    "purpose": "Monthly hosting subscription",
    "business_justification": "Hosting for a customer project",
    "recurring": True,
    "billing_interval": "monthly",
    "monthly_budget": 100.00,
    "previous_payment_reference": None,
    "evidence": [],
}


@pytest.fixture(autouse=True, scope="session")
def _phase1_dev_auth():
    """Authentication is REQUIRED by default (SEC-01). The pre-existing API, episode and
    benchmark tests predate it and exercise risk logic, not authentication, so they run in
    the explicit loopback-only dev mode. Settings read these as defaults, and subprocess
    servers inherit them. The TestClient's default Host header ("testserver") is allowed
    as well. Session scope, so module-scoped fixtures (bench v2 smoke run) see it too.
    tests/test_sec_*.py pass auth_mode="required" explicitly."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AGENTPAL_AUTH_MODE", "disabled_insecure_dev")
        mp.setenv("AGENTPAL_ALLOWED_HOSTS", "127.0.0.1,localhost,::1,testserver")
        mp.delenv("AGENTPAL_ENV", raising=False)
        mp.delenv("AGENTPAL_API_TOKENS_FILE", raising=False)
        yield


@pytest.fixture
def policy():
    return load_policy()


@pytest.fixture
def payload():
    return copy.deepcopy(VALID)


def make_request(**overrides) -> PaymentRequest:
    data = copy.deepcopy(VALID)
    data.update(overrides)
    return PaymentRequest.model_validate(data)


def fixture_spend(req: PaymentRequest | None = None) -> SpendSnapshot:
    """Pure-logic unit tests only: the request's budget fields stand in for operator
    configuration (as in the offline synthetic dataset). The service never does this;
    tests/test_p0_2_spending.py verifies that agent budget fields are ignored there."""
    if req is None:
        return SpendSnapshot.from_synthetic_fixture(VALID["monthly_budget"], 0)
    return SpendSnapshot.from_synthetic_fixture(req.monthly_budget,
                                                req.budget_spent_this_period, req.currency)


def ctx(history=None, now=NOW, spend=None) -> AssessmentContext:
    return AssessmentContext(now=now, history=history or [],
                             spend=spend if spend is not None else fixture_spend())


class FakeAnalyzer:
    """Mocked model backend: returns a fixed analysis, raw text, or raises."""

    name = "fake"
    model_name = "fake-model"
    model_version = "0"
    is_fallback = False
    reports_uncertainty = True

    def __init__(self, analysis: ModelAnalysis | None = None, raw: str | None = None,
                 exc: Exception | None = None, available: bool = True):
        self._analysis, self._raw, self._exc, self._available = analysis, raw, exc, available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def analyze(self, data):
        from agentpal.analyzers import parse_model_output

        self.calls += 1
        if self._exc:
            raise self._exc
        if self._raw is not None:
            return parse_model_output(self._raw)
        return self._analysis


def analysis(score=10, rec="allow_for_review", uncertainty=0.2, findings=None):
    return ModelAnalysis(risk_score=score, recommendation=rec, uncertainty=uncertainty,
                         findings=findings or [], summary="mock")


_REQUESTS: dict[str, PaymentRequest] = {}


def remember(req: PaymentRequest) -> PaymentRequest:
    """Record a request a test assessed, so a later approval can be bound to its exact terms
    (final owner decision 5). The store keeps only a digest, by design."""
    _REQUESTS[req.request_id] = req
    return req


def terms(request_id: str) -> ApprovalTerms:
    return ApprovalTerms.from_request(_REQUESTS[request_id])


__all__ = ["FakeAnalyzer", "AnalyzerUnavailable", "HeuristicAnalyzer", "make_request", "ctx",
           "analysis", "NOW", "VALID"]
