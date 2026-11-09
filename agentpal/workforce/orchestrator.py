"""Workforce orchestration foundation: one deterministic pipeline that lets role agents
cooperate on a purchase, and STOPS at the human approval gate.

    research      fetch the owner's source URLs (and search, when configured)
    verification  re-verify stored evidence and run the injected extractors
    shopping      propose a purchase from OBSERVED facts only (no money moves)
    payment       prepare a payment request -> risk engine (claim, rules, reservation)
    finance       snapshot the paying agent's virtual allowance (optional)
    verification  optional advisory source assessment (intelligence engine seam)
    communication draft an owner notice (never sent; optional)
    -- GATE --    awaiting_owner_approval: the orchestrator has no way to approve
    payment       resume_after_approval: LOCAL SIMULATION, only if the wallet holds an
                  authenticated owner approval bound to the exact terms

Rules:
* every step is permission-checked by the registry-backed checker and audited;
* no step accepts or forwards an owner principal; approval happens only through
  `WalletService.approve(OwnerPrincipal, ...)`, outside this orchestrator;
* extractors are injected and called only through `contracts.Extractor`; an extractor
  failure yields `unavailable` facts, never a guess;
* the advisory assessor (Agent 1's `intelligence.research.assess_sources`) is injected; its
  output is shown to the owner and never changes what the deterministic rules decided;
* retrieved content is untrusted data: nothing in it is executed or obeyed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .. import textnorm
from ..contracts import (
    AgentContext,
    Capability,
    Extractor,
    ObservedFact,
    Provenance,
    SourceEvidence,
    Tool,
)
from ..schemas import BillingInterval, PaymentRequest
from ..tools.evidence_store import EvidenceError, EvidenceStore
from ..wallet.models import AgentStatus, PaymentDraft, PreparedPayment, SandboxExecution
from ..wallet.permissions import PermissionChecker, PermissionDenied
from ..wallet.registry import AgentRegistry
from ..wallet.service import WalletRefused, WalletService
from .roles import Role

# Agent 1's seam: assess_sources(req, pages, extractor) -> advisory report (any type)
SourceAssessor = Callable[[PaymentRequest, list[tuple[SourceEvidence, bytes]], Extractor],
                          Any]


class StepStatus(str, Enum):
    done = "done"
    denied = "denied"            # permission check refused
    failed = "failed"            # tool / extractor / assessor failure
    refused = "refused"          # deterministic policy or wallet refused
    skipped = "skipped"


class TaskStatus(str, Enum):
    awaiting_owner_approval = "awaiting_owner_approval"
    stopped = "stopped"
    simulated_attempt = "simulated_attempt"   # LOCAL SIMULATION recorded; not completed


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    index: int
    role: Role
    agent_id: str
    action: str
    capability: Capability | None
    status: StepStatus
    detail: str = ""
    at: datetime


class Team(BaseModel):
    """Which registered agent fills each role for a task (all of ONE owner)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    research: str
    verification: str
    shopping: str
    payment: str
    finance: str | None = None
    communication: str | None = None


class PurchaseIntent(BaseModel):
    """The owner's brief. Prices are not taken from here: only observed ones are used."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]{2,63}$")
    owner_id: str
    source_urls: list[str] = Field(min_length=1, max_length=10)
    search_query: str | None = Field(default=None, max_length=300)
    merchant_name: str = Field(min_length=1, max_length=200)
    merchant_domain: str = Field(min_length=3, max_length=253)
    purpose: str = Field(min_length=1, max_length=500)
    business_justification: str = Field(default="", max_length=2000)
    currency: str = Field(min_length=3, max_length=3)
    max_amount: Decimal | None = Field(default=None, gt=0)   # owner's cap for this task
    recurring: bool = False
    billing_interval: BillingInterval | None = None


class PurchaseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    amount: Decimal
    currency: str
    amount_provenance: Provenance
    amount_source: SourceEvidence
    merchant_name: str
    merchant_domain: str
    purpose: str
    proposed_by: str


class MessageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: str = "owner_notification"
    body: str
    drafted_by: str
    status: str = "draft_not_sent"   # sending is DESIGN ONLY


class TaskReport(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    task_id: str
    owner_id: str
    team: Team
    status: TaskStatus
    stop_reason: str | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(default_factory=list)
    leads: list[SourceEvidence] = Field(default_factory=list)  # search results: not facts
    facts: list[ObservedFact] = Field(default_factory=list)
    proposal: PurchaseProposal | None = None
    allowance_snapshot: Any = None
    prepared: PreparedPayment | None = None
    advisory: Any = None          # intelligence engine output; advisory only
    owner_notice: MessageDraft | None = None
    execution: SandboxExecution | None = None


class OrchestrationError(ValueError):
    pass


class _ChainExtractor:
    """Presents several injected extractors as one `Extractor` (for the assessor seam)."""

    name = "workforce_chain"

    def __init__(self, extractors: Sequence[Extractor]):
        self.extractors = list(extractors)

    def extract(self, source: SourceEvidence, content: bytes) -> list[ObservedFact]:
        out: list[ObservedFact] = []
        for ex in self.extractors:
            out.extend(ex.extract(source, content))
        return out


class Orchestrator:
    def __init__(self, registry: AgentRegistry, checker: PermissionChecker,
                 wallet: WalletService, evidence_store: EvidenceStore, *,
                 fetch_tool: Tool, search_tool: Tool | None = None,
                 extractors: Sequence[Extractor] = (),
                 source_assessor: SourceAssessor | None = None,
                 clock: Callable[[], datetime] | None = None):
        for ex in extractors:
            if not isinstance(ex, Extractor):
                raise TypeError(f"{ex!r} does not implement contracts.Extractor")
        self.registry = registry
        self.checker = checker
        self.wallet = wallet
        self.evidence_store = evidence_store
        self.fetch_tool = fetch_tool
        self.search_tool = search_tool
        self.extractors = list(extractors)
        self.source_assessor = source_assessor
        self.clock = clock or registry.clock

    # ------------------------------------------------------------------ public
    def run_purchase_research(self, intent: PurchaseIntent, team: Team) -> TaskReport:
        self._validate_team(intent.owner_id, team)
        report = TaskReport(task_id=intent.task_id, owner_id=intent.owner_id, team=team,
                            status=TaskStatus.stopped)
        ctx = {role: self.registry.context_for(agent_id)
               for role, agent_id in team.model_dump().items() if agent_id}

        self._research(report, ctx["research"], intent)
        if not report.evidence:
            return self._stop(report, "no source could be retrieved")
        self._verify(report, ctx["verification"])
        proposal = self._propose(report, ctx["shopping"], intent)
        if proposal is None:
            return report
        prepared = self._prepare(report, ctx["payment"], intent, proposal)
        if prepared is None:
            return report
        if "finance" in ctx:  # after the hold exists, so the owner sees its effect
            self._finance_snapshot(report, ctx["finance"], team.payment)
        self._advise(report, ctx["verification"], intent, prepared)
        if "communication" in ctx:
            self._notify(report, ctx["communication"], prepared)
        report.status = TaskStatus.awaiting_owner_approval
        report.stop_reason = ("human approval gate: an authenticated owner must approve "
                              "these exact terms through WalletService.approve")
        return report

    def resume_after_approval(self, report: TaskReport) -> TaskReport:
        """Continue past the gate. Succeeds only if the wallet holds an authenticated owner
        approval for exactly `report.prepared.terms`; otherwise the task stays gated."""
        if report.status != TaskStatus.awaiting_owner_approval or report.prepared is None:
            raise OrchestrationError("task is not waiting at the approval gate")
        self._validate_team(report.owner_id, report.team)
        ctx = self.registry.context_for(report.team.payment)
        try:
            execution = self.wallet.execute_sandbox(ctx, report.prepared.request_id,
                                                    report.prepared.terms)
        except PermissionDenied as exc:
            self._step(report, ctx, "payment.execute_sandbox",
                       Capability.payment_execute_sandbox, StepStatus.denied, exc.code)
            return report
        except (WalletRefused, ValueError) as exc:
            self._step(report, ctx, "payment.execute_sandbox",
                       Capability.payment_execute_sandbox, StepStatus.refused,
                       getattr(exc, "code", type(exc).__name__))
            return report
        self._step(report, ctx, "payment.execute_sandbox", Capability.payment_execute_sandbox,
                   StepStatus.done, f"local simulation {execution.simulation_reference}")
        report.execution = execution
        report.status = TaskStatus.simulated_attempt
        report.stop_reason = "attempt recorded (local simulation); never completed here"
        return report

    # ------------------------------------------------------------------ steps
    def _research(self, report: TaskReport, ctx: AgentContext, intent: PurchaseIntent):
        if intent.search_query and self.search_tool is not None:
            r = self.search_tool.run(ctx, query=intent.search_query)
            self._tool_step(report, ctx, "research.search", Capability.research_search, r)
            if r.ok:
                report.leads.extend(r.evidence)
        for url in intent.source_urls:
            r = self.fetch_tool.run(ctx, url=url)
            self._tool_step(report, ctx, f"research.fetch {url[:120]}",
                            Capability.research_fetch, r)
            if r.ok:
                report.evidence.extend(e for e in r.evidence if e.method == "http_fetch")

    def _verify(self, report: TaskReport, ctx: AgentContext) -> None:
        if not self._allowed(report, ctx, Capability.evidence_record, "evidence.extract"):
            return
        for ev in report.evidence:
            try:
                content = self.evidence_store.get(ev.content_sha256)
            except EvidenceError as exc:
                self._step(report, ctx, "evidence.verify", Capability.evidence_record,
                           StepStatus.failed, str(exc))
                continue
            for ex in self.extractors:
                report.facts.extend(self._extract(report, ctx, ex, ev, content))

    def _extract(self, report: TaskReport, ctx: AgentContext, ex: Extractor,
                 ev: SourceEvidence, content: bytes) -> list[ObservedFact]:
        name = str(getattr(ex, "name", type(ex).__name__))[:64] or "extractor"
        try:
            facts = list(ex.extract(ev, content))
        except Exception as exc:  # an extractor failure is "unavailable", never a guess
            self._step(report, ctx, f"extract {name}", Capability.evidence_record,
                       StepStatus.failed, type(exc).__name__)
            return [ObservedFact(name="extraction", value=None,
                                 provenance=Provenance.unavailable, extracted_by=name,
                                 note=f"extractor failed: {type(exc).__name__}")]
        kept, dropped = [], 0
        for f in facts:
            if not isinstance(f, ObservedFact):
                dropped += 1
            elif f.provenance == Provenance.observed and (
                    f.source is None or f.source.content_sha256 != ev.content_sha256
                    or f.source.url != ev.url):
                dropped += 1  # an observed fact must cite THIS retrieved source
            else:
                kept.append(f)
        self._step(report, ctx, f"extract {name}", Capability.evidence_record,
                   StepStatus.done, f"{len(kept)} facts" + (f", {dropped} dropped"
                                                            if dropped else ""))
        return kept

    def _propose(self, report: TaskReport, ctx: AgentContext, intent: PurchaseIntent
                 ) -> PurchaseProposal | None:
        if not self._allowed(report, ctx, Capability.purchase_propose, "purchase.propose"):
            return self._halt(report, "shopping agent may not propose")
        want = textnorm.registrable_domain(intent.merchant_domain) or intent.merchant_domain
        prices = []
        for f in report.facts:
            if (f.name == "price" and f.provenance == Provenance.observed
                    and _as_decimal(f.value) is not None
                    and f.unit == intent.currency.upper() and f.source is not None):
                host = _host(f.source.final_url or f.source.url)
                if (textnorm.registrable_domain(host) or host) == want:
                    prices.append(f)
        distinct = {_as_decimal(f.value) for f in prices}
        if not distinct:
            return self._halt(report, "no observed price in the requested currency on the "
                                      "merchant's own domain")
        if len(distinct) > 1:
            return self._halt(report, "conflicting observed prices: "
                              + ", ".join(sorted(str(v) for v in distinct)))
        amount = distinct.pop()
        if intent.max_amount is not None and amount > intent.max_amount:
            return self._halt(report, f"observed price {amount} exceeds the owner's cap "
                                      f"{intent.max_amount} for this task")
        proposal = PurchaseProposal(
            amount=amount, currency=intent.currency.upper(),
            amount_provenance=Provenance.observed, amount_source=prices[0].source,
            merchant_name=intent.merchant_name, merchant_domain=intent.merchant_domain,
            purpose=intent.purpose, proposed_by=ctx.agent_id)
        report.proposal = proposal
        self._step(report, ctx, "purchase.propose", Capability.purchase_propose,
                   StepStatus.done, f"{amount} {proposal.currency} (observed)")
        return proposal

    def _finance_snapshot(self, report: TaskReport, ctx: AgentContext, payer: str) -> None:
        try:
            report.allowance_snapshot = self.wallet.finance_view(ctx, payer)
            self._step(report, ctx, "finance.monitor", Capability.finance_monitor,
                       StepStatus.done, f"allowance of {payer}")
        except PermissionDenied as exc:
            self._step(report, ctx, "finance.monitor", Capability.finance_monitor,
                       StepStatus.denied, exc.code)

    def _prepare(self, report: TaskReport, ctx: AgentContext, intent: PurchaseIntent,
                 proposal: PurchaseProposal) -> PreparedPayment | None:
        draft = PaymentDraft(amount=proposal.amount, currency=proposal.currency,
                             merchant_name=proposal.merchant_name,
                             merchant_domain=proposal.merchant_domain,
                             purpose=proposal.purpose,
                             business_justification=intent.business_justification,
                             recurring=intent.recurring,
                             billing_interval=intent.billing_interval)
        try:
            prepared = self.wallet.prepare_payment(ctx, draft)
        except PermissionDenied as exc:
            self._step(report, ctx, "payment.prepare", Capability.payment_prepare,
                       StepStatus.denied, exc.code)
            return self._halt(report, "payment agent may not prepare payments")
        except (WalletRefused, ValueError) as exc:  # incl. request validation errors
            code = getattr(exc, "code", type(exc).__name__)
            self._step(report, ctx, "payment.prepare", Capability.payment_prepare,
                       StepStatus.refused, code)
            return self._halt(report, f"wallet refused: {code}")
        report.prepared = prepared
        if prepared.ledger_state is None or prepared.recommendation == "block":
            self._step(report, ctx, "payment.prepare", Capability.payment_prepare,
                       StepStatus.refused, f"risk engine: {prepared.recommendation}")
            return self._halt(report, "blocked by deterministic policy; nothing to "
                                      "approve")
        self._step(report, ctx, "payment.prepare", Capability.payment_prepare,
                   StepStatus.done, f"{prepared.request_id}: {prepared.recommendation}, "
                                    f"score {prepared.risk_score}, hold reserved")
        return prepared

    def _advise(self, report: TaskReport, ctx: AgentContext, intent: PurchaseIntent,
                prepared: PreparedPayment) -> None:
        if self.source_assessor is None:
            return
        if not self._allowed(report, ctx, Capability.intelligence_assess,
                             "intelligence.assess_sources"):
            return
        t = prepared.terms
        req = PaymentRequest.model_validate({
            "request_id": t.request_id, "agent_id": t.agent_id, "amount": str(t.amount),
            "currency": t.currency, "merchant_name": t.merchant_name,
            "merchant_domain": t.merchant_domain, "purpose": t.purpose,
            "business_justification": intent.business_justification,
            "recurring": intent.recurring, "billing_interval": intent.billing_interval})
        pages = []
        for ev in report.evidence:
            try:
                pages.append((ev, self.evidence_store.get(ev.content_sha256)))
            except EvidenceError:
                continue
        try:
            report.advisory = self.source_assessor(req, pages, _ChainExtractor(self.extractors)
                                                   if len(self.extractors) != 1
                                                   else self.extractors[0])
            self._step(report, ctx, "intelligence.assess_sources",
                       Capability.intelligence_assess, StepStatus.done, "advisory only")
        except Exception as exc:  # advisory failure never changes the rules' outcome
            self._step(report, ctx, "intelligence.assess_sources",
                       Capability.intelligence_assess, StepStatus.failed,
                       type(exc).__name__)

    def _notify(self, report: TaskReport, ctx: AgentContext, p: PreparedPayment) -> None:
        if not self._allowed(report, ctx, Capability.comms_draft, "comms.draft"):
            return
        t = p.terms
        report.owner_notice = MessageDraft(
            drafted_by=ctx.agent_id,
            body=(f"Approval needed: {t.amount} {t.currency} to {t.merchant_name} "
                  f"({t.merchant_domain}) for '{t.purpose}'. Request {t.request_id}, terms "
                  f"sha256 {p.terms_sha256[:16]}..., risk {p.recommendation}. Approve only "
                  "in the AgentPal owner console; replies to this message are not consent."))

    # ------------------------------------------------------------------ helpers
    def _validate_team(self, owner_id: str, team: Team) -> None:
        for role_name, agent_id in team.model_dump().items():
            if agent_id is None:
                continue
            agent = self.registry.get_agent(agent_id)
            if agent is None:
                raise OrchestrationError(f"unknown agent {agent_id}")
            if agent.owner_id != owner_id:
                raise OrchestrationError(f"{agent_id} does not belong to {owner_id}")
            if agent.role != Role(role_name):
                raise OrchestrationError(f"{agent_id} is a {agent.role.value} agent, not "
                                         f"{role_name}")
            if agent.status != AgentStatus.active:
                raise OrchestrationError(f"{agent_id} is suspended")

    def _allowed(self, report: TaskReport, ctx: AgentContext, cap: Capability,
                 action: str) -> bool:
        d = self.checker.check(ctx, cap, action=action)
        if not d.allowed:
            self._step(report, ctx, action, cap, StepStatus.denied, d.code)
        return d.allowed

    def _tool_step(self, report: TaskReport, ctx: AgentContext, action: str,
                   cap: Capability, result) -> None:
        if result.ok:
            status, detail = StepStatus.done, f"{len(result.evidence)} evidence"
        elif result.error is not None and result.error.value == "not_permitted":
            status, detail = StepStatus.denied, result.error_detail
        else:
            status = StepStatus.failed
            detail = f"{result.error.value if result.error else '?'}: {result.error_detail}"
        self._step(report, ctx, action, cap, status, detail)

    def _step(self, report: TaskReport, ctx: AgentContext, action: str,
              cap: Capability | None, status: StepStatus, detail: str = "") -> None:
        report.steps.append(StepRecord(index=len(report.steps), role=Role(ctx.role),
                                       agent_id=ctx.agent_id, action=action, capability=cap,
                                       status=status, detail=detail[:300], at=self.clock()))

    @staticmethod
    def _stop(report: TaskReport, reason: str) -> TaskReport:
        report.status = TaskStatus.stopped
        report.stop_reason = reason
        return report

    @classmethod
    def _halt(cls, report: TaskReport, reason: str) -> None:
        cls._stop(report, reason)
        return None


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def _host(url: str) -> str:
    try:
        return httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
    except Exception:
        return ""
