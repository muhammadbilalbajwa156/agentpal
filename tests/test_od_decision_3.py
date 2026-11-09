"""Owner decision 3: time-policy defaults and exact boundary behaviour (all relative to the
trusted server receipt time T; client timestamps are untrusted metadata)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentpal.config import load_policy
from agentpal.features import AssessmentContext
from agentpal.orchestrator import assess
from agentpal.schemas import PaymentRequest

from conftest import VALID, fixture_spend

T = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def run(**kw):
    r = PaymentRequest.model_validate({**VALID, **kw})
    return assess(r, AssessmentContext(now=T, spend=fixture_spend(r)), load_policy(), [])


def codes(a):
    return {s.code for s in a.signals}


def test_owner_defaults():
    tp = load_policy().time_policy
    assert (tp.default_ttl_minutes, tp.max_lifetime_hours, tp.max_client_clock_skew_minutes,
            tp.max_client_age_hours) == (60, 48, 10, 24)


@pytest.mark.parametrize("field,offset,code,present", [
    # expiry: expired iff expires_at <= T
    ("expires_at", -S, "REQUEST_EXPIRED", True),
    ("expires_at", timedelta(0), "REQUEST_EXPIRED", True),
    ("expires_at", S, "REQUEST_EXPIRED", False),
    # lifetime: rejected iff expires_at > T + 48 h
    ("expires_at", timedelta(hours=48), "REQUEST_LIFETIME_EXCEEDS_MAX", False),
    ("expires_at", timedelta(hours=48) + S, "REQUEST_LIFETIME_EXCEEDS_MAX", True),
    # skew: flagged iff created_at > T + 10 min
    ("created_at", timedelta(minutes=10), "TIMESTAMP_IN_FUTURE", False),
    ("created_at", timedelta(minutes=10) + S, "TIMESTAMP_IN_FUTURE", True),
    # staleness: flagged iff created_at < T - 24 h
    ("created_at", -timedelta(hours=24), "STALE_REQUEST", False),
    ("created_at", -timedelta(hours=24) - S, "STALE_REQUEST", True),
])
def test_exact_boundaries(field, offset, code, present):
    a = run(**{field: (T + offset).isoformat()})
    assert (code in codes(a)) is present


def test_default_expiry_applied_when_absent():
    assert run().effective_expires_at == T + timedelta(minutes=60)


def test_hard_vs_soft_time_outcomes():
    assert run(expires_at=(T - S).isoformat()).recommendation == "block"
    assert run(expires_at=(T + timedelta(hours=49)).isoformat()).recommendation == "block"
    stale = run(created_at=(T - timedelta(hours=30)).isoformat())
    assert not stale.hard_violations  # staleness/skew are review signals, not blocks
