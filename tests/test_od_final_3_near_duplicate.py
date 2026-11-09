"""Final owner decision 3: a request for the same agent and merchant, inside the configured
duplicate window, whose amount is within max(2% of the larger amount, one minor unit) of an
earlier request triggers review (NEAR_DUPLICATE_PAYMENT_INTENT). It never blocks by itself.

Comparisons are exact Decimal arithmetic and inclusive (difference <= tolerance); the
tolerance is never rounded."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.features import HistoricalPayment
from agentpal.orchestrator import assess
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy
from agentpal.store import AssessmentStore

from conftest import NOW, VALID, ctx, make_request

CODE = "NEAR_DUPLICATE_PAYMENT_INTENT"


def prior(amount, currency="USD", hours_ago=2.0):
    return HistoricalPayment(request_id="req_prior_1", agent_id=VALID["agent_id"],
                             merchant_name=VALID["merchant_name"],
                             merchant_domain=VALID["merchant_domain"],
                             amount=Decimal(amount), currency=currency,
                             created_at=NOW - timedelta(hours=hours_ago))


def flagged(amount, history, currency="USD"):
    a = assess(make_request(amount=amount, currency=currency, monthly_budget=1_000_000),
               ctx(history), load_policy(), [HeuristicAnalyzer()])
    return CODE in {s.code for s in a.signals}, a


@pytest.mark.parametrize("prev,new,expected", [
    # 2% of the larger amount, upward: 102.04 - 100 = 2.04 <= 2.0408; 102.05 -> 2.05 > 2.041
    ("100.00", "102.04", True), ("100.00", "102.05", False),
    # downward: 2% of 100.00 = 2.00 exactly (inclusive)
    ("100.00", "98.00", True), ("100.00", "97.99", False),
    # rounding: 50.00 - 49.00 = 1.00 == 2% of 50.00 (inclusive, not rounded); 50.01 -> 1.0002
    ("49.00", "50.00", True), ("49.00", "50.01", False),
    # one-cent floor: 2% of 0.11 is 0.0022, so the tolerance is 0.01
    ("0.10", "0.11", True), ("0.10", "0.12", False),
])
def test_usd_boundaries_and_rounding(prev, new, expected):
    assert flagged(new, [prior(prev)])[0] is expected


@pytest.mark.parametrize("prev,new,expected", [
    ("50", "51", True), ("50", "52", False),            # one-yen floor (zero-decimal currency)
    ("10000", "10204", True), ("10000", "10205", False),
])
def test_zero_decimal_currency(prev, new, expected):
    assert flagged(new, [prior(prev, currency="JPY")], currency="JPY")[0] is expected


@pytest.mark.parametrize("hours_ago,expected", [(72, True), (72 + 1 / 3600, False)])
def test_duplicate_window_boundary(hours_ago, expected):
    assert load_policy().limits.duplicate_intent_window_hours == 72
    assert flagged("49.01", [prior("49.00", hours_ago=hours_ago)])[0] is expected


@pytest.mark.parametrize("prev_ccy,new_ccy", [("USD", "GBP"), ("GBP", "USD"), ("USD", "JPY")])
def test_currencies_are_never_compared(prev_ccy, new_ccy):
    found, a = flagged("49", [prior("49", currency=prev_ccy)], currency=new_ccy)
    assert not found
    assert "DUPLICATE_PAYMENT_INTENT" not in {s.code for s in a.signals}


def test_near_duplicate_reviews_but_never_blocks():
    found, a = flagged("49.01", [prior("49.00")])
    assert found and a.recommendation == "require_review" and not a.hard_violations


def test_concurrent_near_duplicates_are_flagged_and_never_blocked(tmp_path):
    """Every request reads history after its own claim commits, so all but (at most) the
    first-claimed request see an earlier near-duplicate, whatever the interleaving."""
    sp = SpendPolicy.model_validate({
        "policy_version": "t", "agents": {VALID["agent_id"]: {"owner_id": "o", "currencies": {
            "USD": {"monthly_budget": "100000.00", "rolling_window_limit": "100000.00",
                    "per_transaction_hard_limit": "5000.00",
                    "per_transaction_review_limit": "1000.00"}}}}})
    s = RiskService(load_policy(), AssessmentStore(tmp_path / "nd.db"), [HeuristicAnalyzer()],
                    clock=lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
                    spend_policy=sp)
    n = 8
    reqs = [PaymentRequest.model_validate({**VALID, "request_id": f"req_nd_{i}",
                                           "amount": f"{49 + i / 100:.2f}"}) for i in range(n)]
    barrier, results, errors = threading.Barrier(n), [], []

    def run(r):
        barrier.wait()
        try:
            results.append(s.assess(r))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    ts = [threading.Thread(target=run, args=(r,)) for r in reqs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == n
    near = [a for a in results if CODE in {x.code for x in a.signals}]
    assert len(near) >= n - 1
    assert all(a.recommendation != "block" for a in results)
