"""The /v1 developer API router.

Authentication happens in `api.SecurityGate` before any route runs; it stores an
`AgentCaller` or `OwnerCaller` in the request scope. The security dependencies below only
(a) document the schemes in OpenAPI and (b) enforce the credential TYPE per route. Every
agent-side authorization decision is made by the registry-backed `PermissionChecker`.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.security import HTTPBearer
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..infra_contracts import AuthenticatedOwner, PaymentLifecycle, ProviderEvent
from ..providers.paypal_webhooks import VerifiedEventIgnored, WebhookVerificationError
from ..schemas import ID_PATTERN
from ..wallet.models import AgentStatus
from .errors import PlatformError, not_configured
from .models import (
    AGENT_ID_PATTERN,
    IDEMPOTENCY_KEY_PATTERN,
    KEY_ID_PATTERN,
    WALLET_ID_PATTERN,
    AgentCreate,
    AgentList,
    AgentOut,
    AgentRegistered,
    ApprovalDecision,
    BrowseIn,
    BrowseOut,
    CounterOffer,
    CounterOut,
    PageCheckIn,
    PageCheckOut,
    CredentialIssued,
    CredentialRotate,
    DecisionOut,
    ErrorOut,
    ExecutionOut,
    MeOut,
    PaymentCreate,
    PaymentList,
    PaymentOut,
    TimelineOut,
    PermissionsOut,
    PermissionsSet,
    MarketCheckOut,
    ReconciliationOut,
    TransactionList,
    V1Health,
    WalletCreate,
    WalletList,
    WalletOut,
    WalletUpdate,
    WebhookAck,
    contains_credential,
)
from .runtime import AgentCaller, OwnerCaller, Platform

V1_CALLER_KEY = "agentpal.v1_caller"
WEBHOOK_PATH = "/v1/providers/paypal/webhooks"
MAX_WEBHOOK_BODY_BYTES = 512 * 1024
OPEN_PATHS = frozenset({"/v1/health", WEBHOOK_PATH, "/v1/setup/status", "/v1/setup/owner"})
log = logging.getLogger("agentpal.platform")

agent_scheme = HTTPBearer(
    scheme_name="AgentApiKey", bearerFormat="apk_<key_id>_<secret>", auto_error=False,
    description="Agent API key issued by POST /v1/agents or a rotation. Shown once.")
owner_scheme = HTTPBearer(
    scheme_name="OwnerToken", bearerFormat="apo_<id>_<secret>", auto_error=False,
    description="Owner credential (durable, hashed, expiring, revocable).")

_E = {401: {"model": ErrorOut, "description": "missing or invalid credential"},
      403: {"model": ErrorOut, "description": "wrong credential type or not permitted"},
      404: {"model": ErrorOut, "description": "unknown, or not yours"},
      422: {"description": "invalid request (`{\"error\": \"invalid_request\", ...}`)"},
      429: {"model": ErrorOut, "description": "rate limited"}}
_E409 = {**_E, 409: {"model": ErrorOut, "description": "conflict with current state"}}
_E503 = {**_E409, 503: {"model": ErrorOut, "description": "dependency not configured"}}


def _platform(request: Request) -> Platform:
    return request.app.state.platform


def _caller(request: Request):
    caller = request.scope.get(V1_CALLER_KEY)
    if caller is None:  # unreachable behind SecurityGate; fail closed anyway
        raise PlatformError(401, "authentication_required")
    return caller


_OwnerCred = Annotated[object, Depends(owner_scheme)]
_AgentCred = Annotated[object, Depends(agent_scheme)]


def owner_caller(request: Request, _cred: _OwnerCred) -> AuthenticatedOwner:
    caller = _caller(request)
    if not isinstance(caller, OwnerCaller):
        raise PlatformError(403, "owner_credential_required",
                            audit="owner_credential_required")
    return _platform(request).ensure_owner(caller.owner)


def agent_caller(request: Request, _cred: _AgentCred) -> AgentCaller:
    caller = _caller(request)
    if not isinstance(caller, AgentCaller):
        raise PlatformError(403, "agent_credential_required",
                            audit="agent_credential_required")
    return caller


def any_caller(request: Request, _a: _AgentCred, _o: _OwnerCred):
    caller = _caller(request)
    if isinstance(caller, OwnerCaller):
        _platform(request).ensure_owner(caller.owner)
    return caller


Owner = Annotated[AuthenticatedOwner, Depends(owner_caller)]
Agent = Annotated[AgentCaller, Depends(agent_caller)]
AnyCaller = Annotated[object, Depends(any_caller)]
AgentId = Annotated[str, Path(pattern=AGENT_ID_PATTERN)]
WalletId = Annotated[str, Path(pattern=WALLET_ID_PATTERN)]
KeyId = Annotated[str, Path(pattern=KEY_ID_PATTERN)]
RequestId = Annotated[str, Path(pattern=ID_PATTERN)]


def build_router() -> APIRouter:
    r = APIRouter(prefix="/v1")

    # ------------------------------------------------------------ health
    @r.get("/health", response_model=V1Health, operation_id="v1Health", tags=["meta"],
           openapi_extra={"security": []})
    def v1_health() -> V1Health:
        return V1Health(version=__version__)

    # ------------------------------------------------------------ agents
    @r.post("/agents", response_model=AgentRegistered, status_code=201,
            operation_id="registerAgent", tags=["agents"], responses=_E409)
    def register_agent(body: AgentCreate, owner: Owner, request: Request):
        view, key = _platform(request).agents.register(
            owner, body.name, body.description, body.role, body.capabilities, body.metadata)
        return AgentRegistered(agent=AgentOut(**view), key_id=key.key_id,
                               api_key=key.api_key)

    @r.get("/agents", response_model=AgentList, operation_id="listAgents", tags=["agents"],
           responses=_E)
    def list_agents(owner: Owner, request: Request):
        return AgentList(agents=[AgentOut(**a) for a in _platform(request).agents.list(owner)])

    @r.get("/agents/{agent_id}", response_model=AgentOut, operation_id="getAgent",
           tags=["agents"], responses=_E)
    def get_agent(agent_id: AgentId, owner: Owner, request: Request):
        p = _platform(request)
        return AgentOut(**p.agents.view(p.agents.owned(owner, agent_id)))

    @r.post("/agents/{agent_id}/credentials", response_model=CredentialIssued,
            status_code=201, operation_id="rotateAgentCredential", tags=["agents"],
            responses=_E)
    def rotate_credential(agent_id: AgentId, owner: Owner, request: Request,
                          body: CredentialRotate | None = None):
        body = body or CredentialRotate()
        key, previous = _platform(request).agents.rotate(owner, agent_id,
                                                         body.overlap_seconds)
        return CredentialIssued(agent_id=agent_id, key_id=key.key_id, api_key=key.api_key,
                                previous_key_expires_at=previous)

    @r.delete("/agents/{agent_id}/credentials/{key_id}", status_code=204,
              operation_id="revokeAgentCredential", tags=["agents"], responses=_E)
    def revoke_credential(agent_id: AgentId, key_id: KeyId, owner: Owner, request: Request):
        _platform(request).agents.revoke(owner, agent_id, key_id)
        return Response(status_code=204)

    @r.post("/agents/{agent_id}/suspend", response_model=AgentOut,
            operation_id="suspendAgent", tags=["agents"], responses=_E)
    def suspend_agent(agent_id: AgentId, owner: Owner, request: Request):
        return AgentOut(**_platform(request).agents.set_status(owner, agent_id,
                                                               AgentStatus.suspended))

    @r.post("/agents/{agent_id}/reactivate", response_model=AgentOut,
            operation_id="reactivateAgent", tags=["agents"], responses=_E)
    def reactivate_agent(agent_id: AgentId, owner: Owner, request: Request):
        return AgentOut(**_platform(request).agents.set_status(owner, agent_id,
                                                               AgentStatus.active))

    @r.get("/agents/{agent_id}/permissions", response_model=PermissionsOut,
           operation_id="getAgentPermissions", tags=["permissions"], responses=_E)
    def get_permissions(agent_id: AgentId, owner: Owner, request: Request):
        p = _platform(request)
        return PermissionsOut(**p.agents.permissions(p.agents.owned(owner, agent_id)))

    @r.put("/agents/{agent_id}/permissions", response_model=PermissionsOut,
           operation_id="setAgentPermissions", tags=["permissions"],
           responses={**_E, 400: {"model": ErrorOut,
                                  "description": "above the role ceiling (nothing changed)"}})
    def set_permissions(agent_id: AgentId, body: PermissionsSet, owner: Owner,
                        request: Request):
        return PermissionsOut(**_platform(request).agents.set_permissions(
            owner, agent_id, body.capabilities))

    # ----------------------------------------------------------- wallets
    def _wallet_out(p: Platform, row: dict) -> WalletOut:
        return WalletOut(
            wallet_id=row["wallet_id"], owner_id=row["owner_id"], agent_id=row["agent_id"],
            currency=row["currency"], status=row["status"],
            limits=p.wallets.limits_view(row),
            allowance=p.wallets.allowance(row, p.risk().store, p.clock()),
            created_at=row["created_at"], updated_at=row["updated_at"])

    def _actor(owner: AuthenticatedOwner) -> str:
        return f"{owner.owner_id}|session:{owner.session_id}"

    @r.post("/wallets", response_model=WalletOut, status_code=201,
            operation_id="createWallet", tags=["wallets"],
            responses={**_E409, 400: {"model": ErrorOut,
                                      "description": "invalid or above operator caps"}})
    def create_wallet(body: WalletCreate, owner: Owner, request: Request):
        p = _platform(request)
        row = p.wallets.create(owner.owner_id, _actor(owner), body.agent_id, body.currency,
                               body.monthly_budget, body.per_transaction_limit,
                               body.per_transaction_review_limit, body.rolling_window_limit)
        return _wallet_out(p, row)

    @r.get("/wallets", response_model=WalletList, operation_id="listWallets",
           tags=["wallets"], responses=_E)
    def list_wallets(owner: Owner, request: Request):
        p = _platform(request)
        return WalletList(wallets=[_wallet_out(p, w)
                                   for w in p.wallets.list_owned(owner.owner_id)])

    @r.post("/wallets/freeze-all", response_model=WalletList,
            operation_id="freezeAllWallets", tags=["wallets"], responses=_E)
    def freeze_all_wallets(owner: Owner, request: Request):
        p = _platform(request)
        return WalletList(wallets=[_wallet_out(p, w)
                                   for w in p.wallets.freeze_all(owner.owner_id, _actor(owner))])

    @r.get("/wallets/{wallet_id}", response_model=WalletOut, operation_id="getWallet",
           tags=["wallets"], responses=_E)
    def get_wallet(wallet_id: WalletId, owner: Owner, request: Request):
        p = _platform(request)
        return _wallet_out(p, p.wallets.get_owned(owner.owner_id, wallet_id, _actor(owner)))

    @r.patch("/wallets/{wallet_id}", response_model=WalletOut, operation_id="updateWallet",
             tags=["wallets"],
             responses={**_E, 400: {"model": ErrorOut,
                                    "description": "invalid or above operator caps"}})
    def update_wallet(wallet_id: WalletId, body: WalletUpdate, owner: Owner,
                      request: Request):
        p = _platform(request)
        changes = {"monthly_budget": body.monthly_budget,
                   "per_transaction_hard_limit": body.per_transaction_limit,
                   "per_transaction_review_limit": body.per_transaction_review_limit,
                   "rolling_window_limit": body.rolling_window_limit}
        row = p.wallets.update(owner.owner_id, _actor(owner), wallet_id,
                               {k: v for k, v in changes.items() if v is not None})
        return _wallet_out(p, row)

    @r.post("/wallets/{wallet_id}/freeze", response_model=WalletOut,
            operation_id="freezeWallet", tags=["wallets"], responses=_E)
    def freeze_wallet(wallet_id: WalletId, owner: Owner, request: Request):
        p = _platform(request)
        return _wallet_out(p, p.wallets.set_status(owner.owner_id, _actor(owner),
                                                   wallet_id, "frozen"))

    @r.post("/wallets/{wallet_id}/unfreeze", response_model=WalletOut,
            operation_id="unfreezeWallet", tags=["wallets"], responses=_E)
    def unfreeze_wallet(wallet_id: WalletId, owner: Owner, request: Request):
        p = _platform(request)
        return _wallet_out(p, p.wallets.set_status(owner.owner_id, _actor(owner),
                                                   wallet_id, "active"))

    @r.get("/wallets/{wallet_id}/transactions", response_model=TransactionList,
           operation_id="listWalletTransactions", tags=["wallets"], responses=_E)
    def wallet_transactions(wallet_id: WalletId, owner: Owner, request: Request):
        p = _platform(request)
        w = p.wallets.get_owned(owner.owner_id, wallet_id, _actor(owner))
        return TransactionList(agent_id=w["agent_id"], currency=w["currency"],
                               transactions=p.wallets.transactions(
                                   w["agent_id"], w["currency"], p.risk().store))

    # ---------------------------------------------------------------- me
    @r.get("/me", response_model=MeOut, operation_id="getMe", tags=["me"], responses=_E)
    def get_me(agent: Agent, request: Request):
        p = _platform(request)
        a = p.registry.get_agent(agent.ctx.agent_id)
        prof = p.agents.profile(a.agent_id) or {}
        return MeOut(agent_id=a.agent_id, owner_id=a.owner_id, name=prof.get("name", ""),
                     role=a.role, status=a.status.value,
                     capabilities=sorted(c.value for c in p.registry.granted(a.agent_id)),
                     key_id=agent.key_id)

    def _require_wallet_read(p: Platform, agent: AgentCaller, action: str) -> None:
        from ..contracts import Capability
        p.payments._require(agent.ctx, Capability.wallet_read, action,
                            resource_agent_id=agent.ctx.agent_id)

    @r.get("/me/wallet", response_model=WalletList, operation_id="getMyWallet",
           tags=["me"], responses=_E)
    def my_wallet(agent: Agent, request: Request):
        p = _platform(request)
        _require_wallet_read(p, agent, "v1.me.wallet")
        return WalletList(wallets=[_wallet_out(p, w)
                                   for w in p.wallets.for_agent(agent.ctx.agent_id)])

    @r.get("/me/wallet/transactions", response_model=TransactionList,
           operation_id="listMyTransactions", tags=["me"], responses=_E)
    def my_transactions(agent: Agent, request: Request):
        p = _platform(request)
        _require_wallet_read(p, agent, "v1.me.transactions")
        return TransactionList(agent_id=agent.ctx.agent_id, currency=None,
                               transactions=p.wallets.transactions(
                                   agent.ctx.agent_id, None, p.risk().store))

    # ---------------------------------------------------------- payments
    @r.post("/payment-requests", response_model=PaymentOut, status_code=201,
            operation_id="createPaymentRequest", tags=["payments"],
            responses={**_E409, 200: {"model": PaymentOut,
                                      "description": "idempotent replay (same key, same "
                                                     "body); header Idempotent-Replayed"},
                       400: {"model": ErrorOut,
                             "description": "Idempotency-Key header missing"}},
            openapi_extra={"parameters": [{
                "name": "Idempotency-Key", "in": "header", "required": True,
                "description": "Same agent, key and body replay the stored response.",
                "schema": {"type": "string", "pattern": IDEMPOTENCY_KEY_PATTERN}}]})
    def create_payment(body: PaymentCreate, agent: Agent, request: Request,
                       response: Response):
        # Document a required header while preserving the established missing-header 400.
        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key is None:
            raise PlatformError(400, "idempotency_key_required")
        if (not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, idempotency_key)
                or contains_credential(idempotency_key)):
            raise PlatformError(422, "invalid_request")
        view, replayed = _platform(request).payments.create(agent.ctx, body, idempotency_key)
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return PaymentOut(**view)

    @r.get("/payment-requests", response_model=PaymentList,
           operation_id="listPaymentRequests", tags=["payments"], responses=_E)
    def list_payments(request: Request, caller: AnyCaller):
        desk = _platform(request).payments
        views = (desk.list_for_owner(caller.owner) if isinstance(caller, OwnerCaller)
                 else desk.list_for_agent(caller.ctx))
        return PaymentList(payment_requests=[PaymentOut(**v) for v in views])

    @r.get("/payment-requests/{request_id}", response_model=PaymentOut,
           operation_id="getPaymentRequest", tags=["payments"], responses=_E)
    def get_payment(request_id: RequestId, request: Request, caller: AnyCaller):
        desk = _platform(request).payments
        view = (desk.get_for_owner(caller.owner, request_id)
                if isinstance(caller, OwnerCaller)
                else desk.get_for_agent(caller.ctx, request_id))
        return PaymentOut(**view)

    @r.get("/payment-requests/{request_id}/timeline", response_model=TimelineOut,
           operation_id="getPaymentRequestTimeline", tags=["payments"], responses=_E)
    def payment_timeline(request_id: RequestId, owner: Owner, request: Request):
        return _platform(request).payments.timeline(owner, request_id)

    @r.post("/payment-requests/{request_id}/request-decision", response_model=PaymentOut,
            operation_id="requestOwnerDecision", tags=["payments"], responses=_E409)
    def request_decision(request_id: RequestId, agent: Agent, request: Request):
        return PaymentOut(**_platform(request).payments.request_decision(agent.ctx,
                                                                         request_id))

    @r.get("/approvals", response_model=PaymentList, operation_id="listApprovals",
           tags=["approvals"], responses=_E503)
    def list_approvals(owner: Owner, request: Request):
        return PaymentList(payment_requests=[
            PaymentOut(**v) for v in _platform(request).payments.approvals(owner)])

    @r.post("/payment-requests/{request_id}/approve", response_model=DecisionOut,
            operation_id="approvePaymentRequest", tags=["approvals"], responses=_E503)
    def approve_payment(request_id: RequestId, body: ApprovalDecision, owner: Owner,
                        request: Request):
        return DecisionOut(**_platform(request).payments.approve(owner, request_id,
                                                                 body.terms_sha256))

    @r.post("/payment-requests/{request_id}/reject", response_model=DecisionOut,
            operation_id="rejectPaymentRequest", tags=["approvals"], responses=_E503)
    def reject_payment(request_id: RequestId, owner: Owner, request: Request):
        return DecisionOut(**_platform(request).payments.reject(owner, request_id))

    @r.post("/payment-requests/{request_id}/counter", response_model=CounterOut,
            status_code=201, operation_id="counterPaymentRequest", tags=["approvals"],
            responses={**_E503, 200: {"model": CounterOut,
                                      "description": "idempotent replay of the same "
                                                     "counter; header Idempotent-Replayed"}})
    def counter_payment(request_id: RequestId, body: CounterOffer, owner: Owner,
                        request: Request, response: Response):
        """Owner-only counter-offer ("Buy this instead"). Approves nothing: the original is
        declined and a replacement with the owner's terms is created for the same agent
        through the normal request path (all rules, quotas and ceilings run again). The
        replacement awaits its own exact-terms approval of its own `terms_sha256`; the
        agent can then execute it unchanged, or not at all. Errors: 409
        `not_awaiting_decision`, 409 `already_countered`, 422 `counter_same_terms`, 422
        `invalid_deal`."""
        out, replayed = _platform(request).payments.counter(owner, request_id, body)
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return CounterOut(original=PaymentOut(**out["original"]),
                          replacement=PaymentOut(**out["replacement"]))

    @r.post("/payment-requests/{request_id}/execute", response_model=ExecutionOut,
            operation_id="executePaymentRequest", tags=["payments"],
            responses={**_E503, 502: {"model": ErrorOut, "description": "provider error"}})
    def execute_payment(request_id: RequestId, agent: Agent, request: Request):
        status = _platform(request).payments.execute(agent.ctx, request_id)
        return ExecutionOut(request_id=request_id, status=status)

    @r.get("/payment-requests/{request_id}/reconciliation",
           response_model=ReconciliationOut, operation_id="getReconciliation",
           tags=["payments"], responses=_E503)
    def reconciliation(request_id: RequestId, owner: Owner, request: Request):
        return ReconciliationOut(**_platform(request).payments.reconciliation(owner,
                                                                              request_id))

    @r.post("/payment-requests/{request_id}/capture", response_model=ExecutionOut,
            operation_id="capturePaymentRequest", tags=["payments"], responses=_E503)
    def capture_payment(request_id: RequestId, owner: Owner, request: Request):
        """Capture only after an authenticated provider read verifies exact payer approval."""
        status = _platform(request).payments.capture(owner, request_id)
        return ExecutionOut(request_id=request_id, status=status)

    @r.post("/payment-requests/{request_id}/reconciliation", response_model=ReconciliationOut,
            operation_id="reconcilePaymentRequest", tags=["payments"], responses=_E503)
    def reconcile_payment(request_id: RequestId, owner: Owner, request: Request):
        """Refresh from a verified provider read; GET remains a local status read."""
        return ReconciliationOut(**_platform(request).payments.reconcile(owner, request_id))

    @r.post("/payment-requests/{request_id}/market-check", response_model=MarketCheckOut,
            operation_id="refreshMarketCheck", tags=["payments"], responses=_E503)
    def refresh_market_check(request_id: RequestId, owner: Owner, request: Request):
        """Owner-only: refresh the Channel3 market check with live offers before approving
        (Channel3's product endpoint is free). Advisory only; changes no payment state."""
        return MarketCheckOut(**_platform(request).payments.refresh_market_check(owner,
                                                                                request_id))

    @r.post("/payment-requests/{request_id}/page-check", response_model=PageCheckOut,
            operation_id="checkMerchantPage", tags=["payments"], responses=_E409)
    def check_merchant_page(request_id: RequestId, owner: Owner, request: Request,
                            body: PageCheckIn | None = None):
        """Owner-only read-only merchant evidence; changes no payment state."""
        return _platform(request).payments.check_merchant_page(
            owner, request_id, body.url if body else None)

    @r.post("/research/browse", response_model=BrowseOut, operation_id="browseUrl",
            tags=["research"], responses=_E503)
    def browse_url(body: BrowseIn, agent: Agent, request: Request):
        """Governed browser visit: requires a registry grant for research.fetch."""
        return _platform(request).payments.browse(agent.ctx, body.url)

    # ------------------------------------------- PayPal extensions (one module per owner)
    # P2 refunds, P3 payouts, P4 PayPal operations. Each module only ADDS routes to `r`.
    from .paypal_refunds_router import add_refund_routes
    from .paypal_payouts_router import add_payout_routes
    from .paypal_ops_router import add_ops_routes
    add_refund_routes(r)
    add_payout_routes(r)
    add_ops_routes(r)

    # ---------------------------------------------------------- webhooks
    @r.post("/providers/paypal/webhooks", response_model=WebhookAck,
            operation_id="receivePaypalWebhook", tags=["providers"],
            openapi_extra={
                "security": [],
                "parameters": [{"name": name, "in": "header", "required": True,
                                "schema": {"type": "string"}}
                               for name in ("PAYPAL-TRANSMISSION-ID", "PAYPAL-TRANSMISSION-TIME",
                                            "PAYPAL-TRANSMISSION-SIG", "PAYPAL-CERT-URL",
                                            "PAYPAL-AUTH-ALGO")],
                "requestBody": {
                    "required": True,
                    "description": "Raw PayPal event bytes. Preserve exactly for signature verification.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "additionalProperties": True}}}}},
            responses={400: {"model": ErrorOut, "description": "verification failed"},
                       413: {"model": ErrorOut, "description": "body too large"},
                       503: {"model": ErrorOut, "description": "not configured or retry required"}})
    async def paypal_webhook(request: Request):
        """Signature-verified by the injected PaymentProvider. Never trusted unverified:
        only a verified event from the configured sandbox provider reaches the sink."""
        p = _platform(request)
        provider, sink = p.deps.provider, p.deps.event_sink
        if (provider is None or sink is None
                or getattr(provider, "environment", None) != "sandbox"):
            raise not_configured()
        raw = await request.body()
        if len(raw) > MAX_WEBHOOK_BODY_BYTES:
            raise PlatformError(413, "request_too_large")
        headers = {}
        for key, value in request.scope.get("headers") or []:
            name = key.decode("latin-1").lower()
            if name.startswith("paypal-") and name in headers:
                raise PlatformError(400, "invalid_webhook", audit="webhook_unverified")
            headers[name] = value.decode("latin-1")
        try:
            event = await run_in_threadpool(provider.verify_webhook, headers, raw)
        except VerifiedEventIgnored as exc:
            # Signature verified; the type is not one AgentPal consumes. Nothing is applied.
            log.info("webhook_ignored", extra={"event_type": exc.event_type})
            return WebhookAck(status="ignored")
        except Exception as exc:
            # WebhookVerificationError messages are fixed literals (never header values or
            # body bytes), so they are safe to log and say WHICH check failed.
            reason = str(exc)[:80] if isinstance(exc, WebhookVerificationError) else None
            log.warning("webhook_verification_failed",
                        extra={"error_type": type(exc).__name__, "reason": reason})
            raise PlatformError(400, "invalid_webhook", audit="webhook_unverified") from None
        if (not isinstance(event, ProviderEvent) or event.verified is not True
                or event.provider != provider.name or event.environment != "sandbox"):
            raise PlatformError(400, "invalid_webhook", audit="webhook_unverified")
        try:
            status = PaymentLifecycle(await run_in_threadpool(sink.apply_provider_event, event))
        except Exception as exc:
            # Includes unknown orders: provider event persistence has already audited
            # them. Non-2xx lets PayPal retry after the create response is committed.
            log.warning("webhook_retry_required", extra={"error_type": type(exc).__name__})
            raise PlatformError(503, "webhook_retry_required", audit="webhook_retry_required") from None
        return WebhookAck(status=status)

    from .workspace_router import build_workspace_router
    r.routes.extend(build_workspace_router().routes)
    return r
