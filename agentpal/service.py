"""Risk service: wires policy, analyzers and persistence together.

This is the boundary Phase 2 will call (agent payment endpoint -> assess ->
approval service). It only assesses; it never executes payments or sends
messages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .analyzers import RiskAnalyzer, build_analyzer_chain
from .config import RiskPolicy, Settings, load_policy
from .features import AssessmentContext, effective_expiry
from .orchestrator import DEFAULT_MODEL_TIMEOUT_S, assess
from .schemas import PaymentRequest, RiskAssessment
from .spending import ReservationRequest, SpendPolicy, load_spend_policy, merchant_key
from .store import AssessmentStore

log = logging.getLogger("agentpal.service")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskService:
    def __init__(self, policy: RiskPolicy, store: AssessmentStore,
                 chain: list[RiskAnalyzer], clock: Callable[[], datetime] | None = None,
                 spend_policy: SpendPolicy | None = None,
                 model_timeout_s: float = DEFAULT_MODEL_TIMEOUT_S, *,
                 recovery_wallet_store=None):
        self.policy = policy
        self.model_timeout_s = model_timeout_s
        # Trusted operator budgets/limits (P0.2); never taken from request fields.
        self.spend_policy = spend_policy if spend_policy is not None else load_spend_policy()
        self.store = store
        self.store.approval_validity = timedelta(
            minutes=self.spend_policy.approval_validity_minutes)
        # SEC-16: attempts with an unknown outcome go to reconciliation_required after this
        self.store.attempt_reconcile_after = timedelta(
            minutes=self.spend_policy.attempt_reconcile_after_minutes)
        self.chain = chain
        # Trusted time source: the host's UTC wall clock (keep it NTP-synchronized).
        # Injectable for tests. Client timestamps never replace it (P0.3).
        self.clock = clock or utc_now
        self.store.clock = self.clock  # one trusted time source for service and ledger
        # SEC-09 restart recovery: a claim older than any possible assessment belongs to a
        # worker that died between claim and completion; release its budget hold.
        self.stale_claim_after = timedelta(seconds=max(300.0, 4 * model_timeout_s))
        # Import locally: lifecycle contracts reference the spending/store boundary.
        from .lifecycle.recovery import startup_recovery

        self.recovery_report = startup_recovery(
            self.store, recovery_wallet_store, now=self.clock(),
            stale_claim_after=self.stale_claim_after)
        if self.recovery_report.stale_claims_recovered:
            log.warning("stale_claims_recovered", extra={
                "count": len(self.recovery_report.stale_claims_recovered)})

    @classmethod
    def from_settings(cls, settings: Settings) -> "RiskService":
        return cls(load_policy(settings.policy_path), AssessmentStore(settings.db_path),
                   build_analyzer_chain(settings),
                   spend_policy=load_spend_policy(settings.spend_policy_path),
                   model_timeout_s=settings.model_timeout_seconds)

    def assess(self, req: PaymentRequest) -> RiskAssessment:
        received_at = self.clock()
        if received_at.tzinfo is None:
            raise ValueError("clock must return timezone-aware UTC datetimes")
        received_at = received_at.astimezone(timezone.utc)  # SEC-07: one offset everywhere
        claimed, spend = self.store.claim_request(req, received_at,
                                                  self._reservation_request(req, received_at))
        if not claimed:
            return self._reject_replay(req, received_at)
        owner_id = spend.owner_id if spend is not None else None
        ctx = AssessmentContext(
            now=received_at,
            spend=spend,
            trusted_history=self.store.trusted_history_for(req.agent_id, owner_id),
            shared_trusted_history=(
                self.store.shared_trusted_history_for(req.agent_id, owner_id)
                if self.spend_policy.shares_merchant_history(owner_id) else []),
            history=self.store.history_for(req.request_id, req.agent_id,
                                           req.merchant_name, req.merchant_domain),
        )
        try:
            assessment = assess(req, ctx, self.policy, self.chain, self.model_timeout_s)
        except Exception as exc:
            self.store.fail_request(req, [type(exc).__name__])
            raise
        self.store.complete_request(req, assessment)
        log.info("assessed", extra={
            "request_id": req.request_id, "score": assessment.risk_score,
            "recommendation": assessment.recommendation, "backend": assessment.model.backend})
        return assessment

    def _reservation_request(self, req: PaymentRequest, received_at: datetime
                             ) -> ReservationRequest:
        agent = self.spend_policy.for_agent(req.agent_id)
        return ReservationRequest(
            owner_id=agent.owner_id if agent else None,
            limits=agent.currencies.get(req.currency) if agent else None,
            rolling_window_hours=self.spend_policy.rolling_window_hours,
            expires_at=effective_expiry(req, received_at, self.policy.time_policy),
            merchant_key=merchant_key(req.merchant_name, req.merchant_domain),
            demo_default_budget=self.spend_policy.uses_demo_default(req.agent_id),
        )

    def _reject_replay(self, req: PaymentRequest, received_at: datetime) -> RiskAssessment:
        """Deterministic response for any submission that lost the claim: always `block`
        with DUPLICATE_REQUEST_ID, no model call, no new history or assessment row."""
        ctx = AssessmentContext(now=received_at, request_id_already_claimed=True)
        assessment = assess(req, ctx, self.policy, [])
        self.store.record_replay(req, assessment)
        log.warning("replay_rejected", extra={"request_id": req.request_id})
        return assessment

    def warm_up(self) -> dict[str, bool]:
        """Controlled loading (decision 7): verify digests and stage/load model artifacts
        once, before requests are served. Failures leave the backend unavailable (the
        service then runs degraded); they never make it skip verification."""
        status = {}
        for a in self.chain:
            ok = a.available()
            loader = getattr(a, "_llm", None)
            if ok and loader is not None:
                try:
                    loader()
                except Exception as exc:  # unavailable until fixed; logged, not raised
                    log.error("model_warm_up_failed", extra={"backend": a.name,
                                                             "error_type": type(exc).__name__})
                    ok = False
            status[a.name] = ok
        return status

    def backends_status(self) -> list[dict[str, object]]:
        return [
            {"backend": a.name, "model_name": a.model_name, "model_version": a.model_version,
             "available": a.available(), "is_fallback": a.is_fallback,
             "unavailable_reason": getattr(a, "unavailable_reason", ""),
             "integrity_required": getattr(a, "require_integrity", None),
             "pinned_sha256": getattr(a, "expected_sha256", None)}
            for a in self.chain
        ]
