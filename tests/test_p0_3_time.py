"""P0.3 regression: server receipt time governs expiry and replay windows (probes P03, P06).

Before the fix, duplicate-intent windows were measured from the agent-supplied
`created_at`, so back- or forward-dating a repeated request evaded detection, and a
request without `expires_at` could never expire.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.config import Settings, load_policy
from agentpal.features import AssessmentContext
from agentpal.orchestrator import assess
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def req(**kw) -> PaymentRequest:
    return PaymentRequest.model_validate({**VALID, **kw})


def codes(a) -> set[str]:
    return {s.code for s in a.signals}


def run(r: PaymentRequest, now: datetime = T0):
    return assess(r, AssessmentContext(now=now), load_policy(), [])


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def service(clock: FakeClock) -> RiskService:
    return RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       clock=clock)


# ------------------------------------------------------------------ expiry policy

def test_missing_expiry_gets_conservative_default_ttl():
    policy = load_policy()
    a = run(req())
    assert a.effective_expires_at == T0 + timedelta(minutes=policy.time_policy.default_ttl_minutes)
    assert "REQUEST_EXPIRED" not in a.hard_violations


def test_expired_request_is_blocked():
    a = run(req(created_at=(T0 - timedelta(hours=2)).isoformat(),
                expires_at=(T0 - timedelta(seconds=1)).isoformat()))
    assert "REQUEST_EXPIRED" in a.hard_violations and a.recommendation == "block"


def test_expiry_boundary_equal_to_receipt_time_is_expired():
    a = run(req(expires_at=T0.isoformat()))
    assert "REQUEST_EXPIRED" in a.hard_violations


def test_lifetime_longer_than_maximum_is_rejected():
    max_h = load_policy().time_policy.max_lifetime_hours
    ok = run(req(expires_at=(T0 + timedelta(hours=max_h)).isoformat()))
    assert "REQUEST_LIFETIME_EXCEEDS_MAX" not in ok.hard_violations
    assert ok.effective_expires_at == T0 + timedelta(hours=max_h)
    too_long = run(req(expires_at=(T0 + timedelta(hours=max_h, seconds=1)).isoformat()))
    assert "REQUEST_LIFETIME_EXCEEDS_MAX" in too_long.hard_violations
    assert too_long.recommendation == "block"


def test_client_expiry_cannot_extend_beyond_server_cap():
    a = run(req(expires_at=(T0 + timedelta(hours=1)).isoformat()))
    assert a.effective_expires_at == T0 + timedelta(hours=1)


# --------------------------------------------------------- client timestamps

def test_future_created_at_beyond_skew_flagged_within_skew_not():
    skew = load_policy().time_policy.max_client_clock_skew_minutes
    within = run(req(created_at=(T0 + timedelta(minutes=skew)).isoformat()))
    assert "TIMESTAMP_IN_FUTURE" not in codes(within)
    beyond = run(req(created_at=(T0 + timedelta(minutes=skew, seconds=1)).isoformat()))
    assert "TIMESTAMP_IN_FUTURE" in codes(beyond)


def test_naive_timestamps_rejected():
    with pytest.raises(ValidationError):
        req(created_at="2026-10-01T12:00:00")
    with pytest.raises(ValidationError):
        req(expires_at="2026-10-01T13:00:00")


def test_timezone_offsets_are_equivalent():
    utc = run(req(expires_at="2026-10-01T13:00:00Z"))
    offset = run(req(expires_at="2026-10-01T18:00:00+05:00"))
    assert utc.effective_expires_at == offset.effective_expires_at


# ------------------------------------------------- replay windows use server time

@pytest.mark.parametrize("client_shift", [timedelta(hours=-100), timedelta(hours=+100),
                                          timedelta(days=-400)])
def test_altered_client_timestamp_does_not_evade_duplicate_detection(client_shift):
    clock = FakeClock(T0)
    s = service(clock)
    s.assess(req(request_id="req_time_a"))
    clock.t = T0 + timedelta(minutes=5)
    shifted = (clock.t + client_shift).isoformat()
    second = s.assess(req(request_id="req_time_b", created_at=shifted))
    assert "DUPLICATE_PAYMENT_INTENT" in codes(second), codes(second)


def test_repeated_requests_with_altered_timestamps_all_detected():
    clock = FakeClock(T0)
    s = service(clock)
    s.assess(req(request_id="req_rep_0"))
    for i, delta in enumerate([timedelta(hours=-30), timedelta(minutes=9), timedelta(0)], 1):
        clock.t = T0 + timedelta(minutes=i)
        a = s.assess(req(request_id=f"req_rep_{i}",
                         created_at=(clock.t + delta).isoformat()))
        assert "DUPLICATE_PAYMENT_INTENT" in codes(a)


def test_server_receipt_time_recorded_not_client_time():
    clock = FakeClock(T0)
    s = service(clock)
    a = s.assess(req(request_id="req_rt_1", created_at=(T0 - timedelta(days=3)).isoformat()))
    assert a.assessed_at == T0
    assert s.store.received_at("req_rt_1") == T0


def test_api_rejects_naive_timestamp():
    s = service(FakeClock(T0))
    with TestClient(create_app(service=s, settings=Settings())) as c:
        r = c.post("/assess", json={**VALID, "created_at": "2026-10-01T12:00:00"})
    assert r.status_code == 422
