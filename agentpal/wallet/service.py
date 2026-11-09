"""WalletService: an agent's virtual allowance, its payment requests, owner approval and
LOCAL SIMULATION of execution, built on top of the existing spend policy and ledger.

Authority stays where it already is:
* limits come only from the operator spend policy (`RiskService.spend_policy`);
* a payment request is claimed, reserved and assessed only by `RiskService.assess`
  (deterministic rules decide; the model is advisory);
* ledger state changes only through `AssessmentStore.approve_authenticated` /
  `decline_authenticated` and `begin_execution`, which bind the exact `ApprovalTerms`;
* this module never records a payment outcome: nothing it does can complete spending.

Separation of duties (SEC-15/SEC-23): `approve`/`decline` accept only an authenticated
owner: an `infra_contracts.AuthenticatedOwner`, re-validated against the durable identity
store on every call, or (unless `require_authenticated_owner` is set) a legacy
`OwnerPrincipal` minted from an operator token. Never an `AgentContext`, never the agent
that made the request. The decision is recorded as a structured record (owner_id,
session_id, auth method) in the ledger database. `execute_sandbox` consumes that
single-use, time-limited approval for exactly the presented terms in the same transaction
that records the attempt.
"""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from ..contracts import AgentContext, Capability
from ..infra_contracts import AuthenticatedOwner, OwnerAuthError, PaymentLifecycle
from ..lifecycle import PaymentLifecycleService
from ..lifecycle.recovery import ConsistencyError, ConsistencyReport, check_consistency
from ..schemas import PaymentRequest
from ..service import RiskService
from ..spending import ApprovalTerms, ApprovalTermsMismatch, LedgerState, budget_period
from .models import (
    AgentIdentity,
    ApprovalRecord,
    CurrencyAllowance,
    LedgerEventView,
    PaymentDraft,
    PreparedPayment,
    SandboxExecution,
    TransactionRecord,
    WalletView,
)
from .permissions import PermissionChecker
from .principals import NotAuthorized, OwnerPrincipal, require_owner
from .registry import AgentRegistry
from .sandbox import LocalSandboxSimulator


_NO_OWNER = "-no-such-owner-"


class WalletRefused(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def new_request_id() -> str:
    """Server-generated, unguessable request id (SEC-13 mitigation). Matches both
    schemas.ID_PATTERN and contracts' id pattern."""
    return "pay_" + uuid.uuid4().hex


class WalletService:
    def __init__(self, registry: AgentRegistry, checker: PermissionChecker,
                 risk_service: RiskService, *,
                 simulator: LocalSandboxSimulator | None = None,
                 id_factory: Callable[[], str] = new_request_id,
                 identity=None, require_authenticated_owner: bool = False):
        self.registry = registry
        self.checker = checker
        self.risk = risk_service
        self.simulator = simulator or LocalSandboxSimulator()
        self.id_factory = id_factory
        # agentpal.identity.OwnerIdentityService: re-validates AuthenticatedOwner sessions
        self.identity = identity
        # True for the /v1 wiring: legacy operator-token OwnerPrincipals are refused
        self.require_authenticated_owner = require_authenticated_owner
        self.lifecycle = PaymentLifecycleService(risk_service.store, clock=self._now)
        # RiskService recovers a process restart. Constructing this facade only checks
        # the second database; an active in-process attempt must not become a restart.
        issues = check_consistency(risk_service.store, registry.store)
        if issues:
            raise ConsistencyError(ConsistencyReport(issues=issues))

    @property
    def ledger(self):
        return self.risk.store

    def _now(self) -> datetime:
        return self.risk.clock()

    # ------------------------------------------------------------ reading
    def view(self, ctx: AgentContext) -> WalletView:
        """An agent reads its OWN virtual allowance (wallet.read)."""
        self.checker.require(ctx, Capability.wallet_read, resource_agent_id=ctx.agent_id,
                             action="wallet.view")
        return self._view(self._agent(ctx.agent_id))

    def finance_view(self, ctx: AgentContext, agent_id: str) -> WalletView:
        """A finance agent monitors another agent of the SAME owner (finance.monitor)."""
        target = self.registry.get_agent(agent_id)
        # an unknown id is denied exactly like another owner's agent (no existence oracle)
        self.checker.require(ctx, Capability.finance_monitor,
                             resource_owner_id=target.owner_id if target else _NO_OWNER,
                             action="wallet.finance_view")
        return self._view(target)

    def history(self, ctx: AgentContext, agent_id: str | None = None
                ) -> list[TransactionRecord]:
        """Wallet-prepared payment requests and their ledger history. Own history needs
        wallet.read; another agent's (same owner only) needs finance.monitor."""
        target_id = agent_id or ctx.agent_id
        if target_id == ctx.agent_id:
            self.checker.require(ctx, Capability.wallet_read,
                                 resource_agent_id=target_id, action="wallet.history")
        else:
            other = self.registry.get_agent(target_id)
            self.checker.require(ctx, Capability.finance_monitor,
                                 resource_owner_id=other.owner_id if other else _NO_OWNER,
                                 action="wallet.history")
        target = self._agent(target_id)
        rows = self.registry.store.query(
            "SELECT * FROM wallet_payment_requests WHERE agent_id=? ORDER BY created_at, "
            "request_id", (target.agent_id,))
        out = []
        for r in rows:
            terms = ApprovalTerms.model_validate_json(r["terms_json"])
            key = dict(agent_id=r["agent_id"])
            entry = self.ledger.ledger_entry(r["request_id"], **key)
            events = [LedgerEventView(from_state=e["from_state"], to_state=e["to_state"],
                                      actor=e["actor"], reason=e["reason"], at=e["at"])
                      for e in self.ledger.ledger_events(r["request_id"], **key)]
            out.append(TransactionRecord(
                request_id=r["request_id"], agent_id=r["agent_id"], currency=terms.currency,
                amount=terms.amount,
                state=LedgerState(entry["state"]) if entry else None,
                period=entry["period"] if entry else None,
                recommendation=r["recommendation"], events=events))
        return out

    def _view(self, agent: AgentIdentity) -> WalletView:
        sp = self.risk.spend_policy
        base = dict(agent_id=agent.agent_id, owner_id=agent.owner_id, role=agent.role,
                    policy_configuration=sp.configuration,
                    instruments=self.registry.instruments_of(agent.owner_id))
        refusal = self._policy_refusal(agent)
        if refusal:
            return WalletView(**base, allowances=[], refusal=refusal)
        policy = sp.for_agent(agent.agent_id)
        now = self._now()
        allowances = []
        for currency, lim in sorted(policy.currencies.items()):
            b = self.ledger.spend_breakdown(agent.agent_id, currency, now)
            allowances.append(CurrencyAllowance(
                currency=currency, period=budget_period(now),
                monthly_budget=lim.monthly_budget,
                per_transaction_hard_limit=lim.per_transaction_hard_limit,
                per_transaction_review_limit=lim.per_transaction_review_limit,
                rolling_window_limit=lim.rolling_window_limit,
                reserved=b["reserved"], approved=b["approved"], attempted=b["attempted"],
                completed=b["completed"], released=b["released"], failed=b["failed"],
                cancelled=b["cancelled"],
                reconciliation_required=b["reconciliation_required"],
                reversed=b["reversed"], committed=b["committed"],
                available=max(Decimal(0), lim.monthly_budget - b["committed"]),
                demo_default_budget=sp.uses_demo_default(agent.agent_id)))
        return WalletView(**base, allowances=allowances)

    def _policy_refusal(self, agent: AgentIdentity) -> str | None:
        policy = self.risk.spend_policy.for_agent(agent.agent_id)
        if policy is None:
            return "NO_SPENDING_POLICY"
        if policy.owner_id != agent.owner_id:
            return "OWNER_MISMATCH"   # registry and operator spend policy disagree
        return None

    # ---------------------------------------------------- payment requests
    def prepare_payment(self, ctx: AgentContext, draft: PaymentDraft) -> PreparedPayment:
        """Build a payment request from an agent's draft and submit it to the risk engine,
        which claims it, applies the deterministic limits and (if within them) reserves the
        amount. The result always requires owner approval."""
        self.checker.require(ctx, Capability.payment_prepare, resource_agent_id=ctx.agent_id,
                             action="wallet.prepare_payment")
        agent = self._agent(ctx.agent_id)
        refusal = self._policy_refusal(agent)
        if refusal:
            raise WalletRefused(refusal, agent.agent_id)
        request_id = self.id_factory()
        data = draft.model_dump(exclude_none=True)
        req = PaymentRequest.model_validate({**data, "amount": str(draft.amount),
                                             "request_id": request_id,
                                             "agent_id": agent.agent_id})
        assessment = self.risk.assess(req)
        terms = ApprovalTerms.from_request(req)
        with self.registry.store.tx() as c:
            c.execute("INSERT INTO wallet_payment_requests (request_id, agent_id, owner_id, "
                      "terms_json, terms_sha256, recommendation, risk_score, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (request_id, agent.agent_id, agent.owner_id,
                       terms.model_dump_json(), terms.digest(),
                       str(getattr(assessment.recommendation, "value",
                                   assessment.recommendation)),
                       assessment.risk_score, self._now().isoformat()))
        return self._prepared(request_id)

    def _prepared(self, request_id: str) -> PreparedPayment:
        rows = self.registry.store.query(
            "SELECT * FROM wallet_payment_requests WHERE request_id=?", (request_id,))
        if not rows:
            raise WalletRefused("UNKNOWN_REQUEST", request_id)
        r = rows[0]
        key = dict(agent_id=r["agent_id"])
        return PreparedPayment(request_id=request_id, agent_id=r["agent_id"],
                               owner_id=r["owner_id"],
                               terms=ApprovalTerms.model_validate(json.loads(r["terms_json"])),
                               terms_sha256=r["terms_sha256"],
                               recommendation=r["recommendation"],
                               risk_score=r["risk_score"],
                               ledger_state=self.ledger.ledger_state(request_id, **key),
                               lifecycle=self.lifecycle.status(request_id, **key))

    def status(self, request_id: str) -> PaymentLifecycle:
        """The request's status as a PaymentLifecycle (callers check access first)."""
        return self._prepared(request_id).lifecycle

    def pending_approvals(self, owner: AuthenticatedOwner | OwnerPrincipal
                          ) -> list[PreparedPayment]:
        """What the owner is asked to decide: their requests still holding a reservation."""
        owner = self._owner(owner)
        rows = self.registry.store.query(
            "SELECT request_id FROM wallet_payment_requests WHERE owner_id=? "
            "ORDER BY created_at, request_id", (owner.owner_id,))
        out = [self._prepared(r["request_id"]) for r in rows]
        return [p for p in out if p.ledger_state == LedgerState.reserved]

    # --------------------------------------------------------- owner only
    def _owner(self, actor: object) -> OwnerPrincipal:
        """Authenticate the actor as an owner, or refuse (SEC-15/SEC-23)."""
        if isinstance(actor, AuthenticatedOwner):
            if self.identity is None:
                raise NotAuthorized("OWNER_SESSION_UNVERIFIABLE",
                                    "no identity service to re-validate the session")
            try:
                self.identity.assert_session_active(actor)
            except OwnerAuthError:
                self._log_refused_actor(actor)
                raise NotAuthorized("INVALID_OWNER_SESSION") from None
            return OwnerPrincipal.from_authenticated(actor)
        if isinstance(actor, OwnerPrincipal):
            if self.require_authenticated_owner:
                raise NotAuthorized("DURABLE_OWNER_AUTH_REQUIRED",
                                    "an operator-token owner cannot decide here")
            if actor.authenticated_via != "operator_token":
                # only an AuthenticatedOwner (re-validated above) may claim a durable session
                raise NotAuthorized("INVALID_OWNER_SESSION", "unverified session claim")
            return actor
        return require_owner(actor)  # an AgentContext / agent principal stops here

    def approve(self, owner: AuthenticatedOwner | OwnerPrincipal, request_id: str,
                terms: ApprovalTerms | None = None, *,
                terms_sha256: str | None = None) -> ApprovalRecord:
        """Approve exactly `terms` (or the prepared terms whose SHA-256 is `terms_sha256`).
        Returns the structured approval record: single-use, expiring at
        min(now + approval validity, request expiry)."""
        return ApprovalRecord.model_validate(
            self._decide(owner, request_id, "approved", terms, terms_sha256))

    def decline(self, owner: AuthenticatedOwner | OwnerPrincipal, request_id: str) -> None:
        self._decide(owner, request_id, "declined", None, None)

    def _decide(self, actor: object, request_id: str, decision: str,
                terms: ApprovalTerms | None, terms_sha256: str | None) -> dict | None:
        owner = self._owner(actor)
        prepared = self._prepared(request_id)
        key = dict(agent_id=prepared.agent_id)
        entry = self.ledger.ledger_entry(request_id, **key)
        record = None
        try:
            if prepared.owner_id != owner.owner_id:
                raise NotAuthorized("CROSS_OWNER", "request belongs to another owner")
            if entry is None:
                raise WalletRefused("NO_HOLD", "no reservation exists (blocked by policy)")
            if entry["owner_id"] != owner.owner_id:
                raise NotAuthorized("CROSS_OWNER", "ledger entry belongs to another owner")
            if owner.owner_id == prepared.agent_id:
                raise NotAuthorized("SELF_APPROVAL", "an agent cannot decide its own request")
            ident = dict(owner_id=owner.owner_id, session_id=owner.token_id,
                         auth_method=owner.authenticated_via, now=self._now(), **key)
            if decision == "approved":
                if terms_sha256 is not None:
                    if not isinstance(terms_sha256, str) or not hmac.compare_digest(
                            terms_sha256, prepared.terms_sha256):
                        raise ApprovalTermsMismatch("terms digest differs from the request")
                if terms is None and terms_sha256 is not None:
                    terms = prepared.terms
                if not isinstance(terms, ApprovalTerms):
                    raise WalletRefused("TERMS_REQUIRED")
                record = self.ledger.approve_authenticated(
                    request_id, terms, org_id=owner.org_id,
                    mfa_satisfied=owner.mfa_satisfied, **ident)
            else:
                self.ledger.decline_authenticated(request_id, **ident)
        except Exception as exc:
            self._log_decision(owner, request_id, "refused", terms,
                               f"{decision}:{type(exc).__name__}:{getattr(exc, 'code', '')}")
            raise
        self._log_decision(owner, request_id, decision, terms, None)
        return record

    def _log_refused_actor(self, actor: AuthenticatedOwner) -> None:
        with self.registry.store.tx() as c:
            c.execute("INSERT INTO wallet_owner_decisions (request_id, owner_id, token_id, "
                      "decision, terms_sha256, detail, at) VALUES (?,?,?,?,?,?,?)",
                      ("-", actor.owner_id, actor.session_id[:64], "refused", None,
                       "INVALID_OWNER_SESSION", self._now().isoformat()))

    def _log_decision(self, owner: OwnerPrincipal, request_id: str, decision: str,
                      terms: ApprovalTerms | None, detail: str | None) -> None:
        with self.registry.store.tx() as c:
            c.execute("INSERT INTO wallet_owner_decisions (request_id, owner_id, token_id, "
                      "decision, terms_sha256, detail, at) VALUES (?,?,?,?,?,?,?)",
                      (request_id, owner.owner_id, owner.token_id, decision,
                       terms.digest() if isinstance(terms, ApprovalTerms) else None,
                       detail, self._now().isoformat()))

    def owner_decisions(self, request_id: str) -> list[dict]:
        return self.registry.store.query("SELECT * FROM wallet_owner_decisions WHERE "
                                         "request_id=? ORDER BY id", (request_id,))

    # ------------------------------------------------- local simulation only
    def execute_sandbox(self, ctx: AgentContext, request_id: str,
                        terms: ApprovalTerms) -> SandboxExecution:
        """LOCAL SIMULATION of submitting an owner-approved payment. Consumes the
        single-use authenticated approval and records a payment ATTEMPT bound to the exact
        terms (with persisted idempotency keys); the entry stays `attempted`, never
        completed."""
        prepared = self._prepared(request_id)
        self.checker.require(ctx, Capability.payment_execute_sandbox,
                             resource_owner_id=prepared.owner_id,
                             resource_agent_id=prepared.agent_id,
                             action="wallet.execute_sandbox")
        if not isinstance(terms, ApprovalTerms) or terms.digest() != prepared.terms_sha256:
            raise WalletRefused("TERMS_MISMATCH", "terms differ from the prepared request")
        key = dict(agent_id=prepared.agent_id)
        approvals = [a for a in self.ledger.approvals(request_id, **key)
                     if a["owner_id"] == prepared.owner_id
                     and hmac.compare_digest(a["terms_sha256"], terms.digest())]
        if not approvals:
            raise WalletRefused("NO_OWNER_APPROVAL",
                                "no authenticated owner approval for these exact terms")
        if self.ledger.ledger_state(request_id, **key) != LedgerState.approved:
            raise WalletRefused("NOT_APPROVED", "the ledger entry is not in state approved")
        if self.registry.store.query("SELECT 1 FROM wallet_sandbox_simulations WHERE "
                                     "request_id=?", (request_id,)):
            raise WalletRefused("ALREADY_SUBMITTED")
        # consumes the approval; refuses an expired or already used one (decision 6, SEC-15)
        ticket = self.lifecycle.begin_execution(prepared.agent_id, request_id, terms,
                                                self.simulator.provider)
        reference = self.simulator.submit(terms)
        self.ledger.record_provider_order(request_id, agent_id=prepared.agent_id,
                                          provider=self.simulator.provider,
                                          provider_order_id=reference,
                                          idempotency_key=ticket.create_key,
                                          status="created", now=self._now())
        with self.registry.store.tx() as c:
            c.execute("INSERT INTO wallet_sandbox_simulations (request_id, agent_id, "
                      "simulation_reference, terms_sha256, at) VALUES (?,?,?,?,?)",
                      (request_id, ctx.agent_id, reference, terms.digest(),
                       self._now().isoformat()))
        return SandboxExecution(request_id=request_id, simulation_reference=reference,
                                ledger_state=self.ledger.ledger_state(request_id, **key))

    # ------------------------------------------------------------ helpers
    def _agent(self, agent_id: str) -> AgentIdentity:
        agent = self.registry.get_agent(agent_id)
        if agent is None:
            raise WalletRefused("UNKNOWN_AGENT", agent_id)
        return agent
