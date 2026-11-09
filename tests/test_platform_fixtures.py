"""Shared fixtures for the /v1 developer platform tests (no tests here).

Fakes stand in for the injected protocols (owner authentication and approval/lifecycle are
Specialist 1's, the PayPal sandbox provider is Specialist 4's). The fakes drive the REAL
ledger (`AssessmentStore`) through its public methods, so allowance figures and state
changes seen through /v1 are the ledger's own."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings, load_policy
from agentpal.infra_contracts import (
    AuthenticatedOwner,
    OwnerAuthError,
    PaymentLifecycle,
    ProviderError,
    ProviderEvent,
    ProviderOrderRef,
    ProviderStatus,
)
from agentpal.platform import PlatformDeps, PlatformLimits
from agentpal.service import RiskService
from agentpal.spending import ApprovalTerms, LedgerState, SpendPolicy
from agentpal.store import AssessmentStore
from agentpal.wallet.store import WalletStore

START = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
OWNER_A = "apo_ownera_" + "a" * 40
OWNER_B = "apo_ownerb_" + "b" * 40
OWNER_DOTTED = "apo_dotted_" + "d" * 40
LEGACY_AGENT = "apt_agent_legacy_" + "l" * 40
LEGACY_OPS = "apt_operator_" + "o" * 40
WEBHOOK_SECRET = b"fake-webhook-secret"


class Clock:
    def __init__(self, now: datetime = START):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class FakeOwnerAuth:
    """OwnerAuthenticator fake: a fixed map of owner credentials."""

    def __init__(self, clock: Clock, mfa: bool = False):
        self.clock = clock
        self.owners = {OWNER_A: "owner_alice", OWNER_B: "owner_bob",
                       OWNER_DOTTED: "owner.dotted"}
        self.mfa = mfa
        self.calls = 0

    def authenticate(self, bearer: str) -> AuthenticatedOwner:
        self.calls += 1
        owner_id = self.owners.get(bearer)
        if owner_id is None:
            raise OwnerAuthError("invalid")
        return AuthenticatedOwner(owner_id=owner_id, session_id=f"sess_{owner_id[:20]}x",
                                  auth_method="owner_api_token",
                                  authenticated_at=self.clock(), mfa_satisfied=self.mfa)


class FakeApprovals:
    """ApprovalGateway fake: binds the digest and records the decision in the ledger."""

    def __init__(self, ledger: AssessmentStore, wallet_store: WalletStore):
        self.ledger = ledger
        self.ws = wallet_store
        self.calls: list[tuple] = []

    def _terms(self, request_id: str) -> tuple[ApprovalTerms, str, str]:
        r = self.ws.query("SELECT * FROM wallet_payment_requests WHERE request_id=?",
                          (request_id,))[0]
        return ApprovalTerms.model_validate_json(r["terms_json"]), r["terms_sha256"], \
            r["owner_id"]

    def approve(self, owner: AuthenticatedOwner, request_id: str, *, terms_sha256: str):
        self.calls.append(("approve", owner.owner_id, request_id, terms_sha256))
        terms, digest, owner_id = self._terms(request_id)
        if owner_id != owner.owner_id:
            raise PermissionError("cross owner")
        if digest != terms_sha256:
            raise ValueError("terms mismatch")
        self.ledger.record_owner_decision(request_id, "approved",
                                          f"{owner.owner_id}|session:{owner.session_id}",
                                          terms=terms)

    def decline(self, owner: AuthenticatedOwner, request_id: str):
        self.calls.append(("decline", owner.owner_id, request_id))
        _, _, owner_id = self._terms(request_id)
        if owner_id != owner.owner_id:
            raise PermissionError("cross owner")
        self.ledger.record_owner_decision(request_id, "declined",
                                          f"{owner.owner_id}|session:{owner.session_id}")


class FakeLifecycle:
    """LifecycleGateway fake (TEST ONLY): a minimal ledger-state mapping."""

    def __init__(self, ledger: AssessmentStore, wallet_store: WalletStore):
        self.ledger = ledger
        self.ws = wallet_store
        self.unknown: set[str] = set()
        self.refs: dict[str, ProviderOrderRef] = {}
        self.begun: list[str] = []

    def status(self, request_id: str, *, agent_id: str | None = None) -> PaymentLifecycle:
        if request_id in self.unknown:
            return PaymentLifecycle.reconciliation_required
        entry = self.ledger.ledger_entry(request_id)
        if entry is None:
            return PaymentLifecycle.rejected
        state = LedgerState(entry["state"])
        if state == LedgerState.reserved:
            meta = self.ws.query("SELECT decision_requested FROM platform_payment_meta "
                                 "WHERE request_id=?", (request_id,))
            if meta and not meta[0]["decision_requested"]:
                return PaymentLifecycle.prepared
        if state == LedgerState.released:
            reasons = [e["reason"] for e in self.ledger.ledger_events(request_id)]
            return (PaymentLifecycle.expired if reasons and reasons[-1].endswith("expired")
                    else PaymentLifecycle.rejected)
        return {LedgerState.reserved: PaymentLifecycle.awaiting_approval,
                LedgerState.approved: PaymentLifecycle.approved,
                LedgerState.pending: PaymentLifecycle.submitted_to_provider,
                LedgerState.completed: PaymentLifecycle.provider_confirmed,
                LedgerState.failed: PaymentLifecycle.failed,
                LedgerState.cancelled: PaymentLifecycle.failed}[state]

    def begin_execution(self, agent_id, request_id, terms, provider):
        if self.ledger.ledger_state(request_id) != LedgerState.approved:
            raise ValueError("not approved")
        if terms.agent_id != agent_id:
            raise PermissionError("not your request")
        self.begun.append(request_id)
        return {"request_id": request_id, "terms": terms,
                "idempotency_key": "exec-" + request_id}

    def submit_to_provider(self, ticket, provider) -> PaymentLifecycle:
        rid, terms = ticket["request_id"], ticket["terms"]
        try:
            ref = provider.create_order(terms, idempotency_key=ticket["idempotency_key"])
        except ProviderError as exc:
            if exc.outcome_unknown:
                self.unknown.add(rid)
                return PaymentLifecycle.reconciliation_required
            raise
        self.refs[rid] = ref
        self.ledger.record_payment_attempt(rid, provider.name, ref.provider_order_id,
                                           terms=terms)
        return PaymentLifecycle.submitted_to_provider

    def reconcile(self, request_id: str) -> PaymentLifecycle:
        return self.status(request_id)

    def provider_references(self, request_id: str) -> list[ProviderOrderRef]:
        return [self.refs[request_id]] if request_id in self.refs else []


class FakeProvider:
    name = "paypal"
    environment = "sandbox"

    def __init__(self):
        self.orders: list[tuple] = []
        self.fail_unknown = False
        self.seq = itertools.count(1)

    def create_order(self, terms, *, idempotency_key, return_url=None) -> ProviderOrderRef:
        if self.fail_unknown:
            raise ProviderError("timeout", outcome_unknown=True)
        oid = f"ORDER-{next(self.seq):04d}"
        self.orders.append((oid, terms, idempotency_key))
        return ProviderOrderRef(provider="paypal", environment="sandbox",
                                provider_order_id=oid, idempotency_key=idempotency_key,
                                status=ProviderStatus.payer_action_required,
                                payer_action_url="https://www.sandbox.paypal.com/x")

    def capture_order(self, provider_order_id, *, idempotency_key):  # pragma: no cover
        raise NotImplementedError

    def get_order_status(self, provider_order_id):  # pragma: no cover
        raise NotImplementedError

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> ProviderEvent:
        sig = headers.get("x-fake-signature", "")
        good = hmac.new(WEBHOOK_SECRET, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            raise ValueError("bad signature")
        data = json.loads(raw_body)
        return ProviderEvent(provider=data.get("provider", "paypal"), environment="sandbox",
                             event_id=data["id"], event_type="PAYMENT.CAPTURE.COMPLETED",
                             provider_order_id=data["order"],
                             status=ProviderStatus.completed,
                             verified=data.get("verified", True),
                             received_at=START,
                             raw_sha256=hashlib.sha256(raw_body).hexdigest())


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()


class FakeSink:
    def __init__(self):
        self.events: list[ProviderEvent] = []

    def apply_provider_event(self, event: ProviderEvent) -> PaymentLifecycle:
        self.events.append(event)
        return PaymentLifecycle.provider_confirmed


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def settings(**kw) -> Settings:
    base = dict(auth_mode="required", api_tokens_file=None,
                allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],
                rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=1000)
    base.update(kw)
    return Settings(**base)


@dataclass
class World:
    client: TestClient
    app: object
    risk: RiskService
    deps: PlatformDeps
    clock: Clock
    owner_auth: FakeOwnerAuth
    approvals: FakeApprovals
    lifecycle: FakeLifecycle
    provider: FakeProvider
    sink: FakeSink
    wallet_store: WalletStore
    idem: itertools.count = field(default_factory=lambda: itertools.count(1))

    @property
    def platform(self):
        return self.app.state.platform

    # ------------------------------------------------------------ helpers
    @staticmethod
    def h(token: str, **extra) -> dict:
        return {"Authorization": f"Bearer {token}", **extra}

    def register(self, owner: str = OWNER_A, **body) -> tuple[str, str]:
        r = self.client.post("/v1/agents", json={"name": "bot", **body}, headers=self.h(owner))
        assert r.status_code == 201, r.text
        return r.json()["agent"]["agent_id"], r.json()["api_key"]

    def grant(self, agent_id: str, caps: list[str], owner: str = OWNER_A) -> None:
        r = self.client.put(f"/v1/agents/{agent_id}/permissions", json={"capabilities": caps},
                            headers=self.h(owner))
        assert r.status_code == 200, r.text

    def wallet(self, agent_id: str, owner: str = OWNER_A, **body) -> dict:
        payload = {"agent_id": agent_id, "currency": "USD", "monthly_budget": "100.00",
                   "per_transaction_limit": "60.00", **body}
        r = self.client.post("/v1/wallets", json=payload, headers=self.h(owner))
        assert r.status_code == 201, r.text
        return r.json()

    def pay(self, key: str, idem: str | None = None, **body):
        payload = {"amount": "49.99", "currency": "USD", "merchant_name": "Example Hosting",
                   "merchant_domain": "example.com", "purpose": "Monthly hosting plan",
                   **body}
        idem = idem or f"idem-{next(self.idem):08d}"
        return self.client.post("/v1/payment-requests", json=payload,
                                headers=self.h(key, **{"Idempotency-Key": idem}))

    def funded_agent(self, owner: str = OWNER_A, caps: list[str] | None = None,
                     **wallet) -> tuple[str, str, dict]:
        agent_id, key = self.register(owner)
        if caps is not None:
            self.grant(agent_id, caps, owner)
        w = self.wallet(agent_id, owner, **wallet)
        return agent_id, key, w


ALL_PAY = ["wallet.read", "payment.prepare", "owner.request_decision",
           "payment.execute_sandbox"]


def build_world(*, mfa: bool = False, require_mfa: bool = False, limits=None,
                configured: bool = True, market_checker=None, **settings_kw) -> World:
    clock = Clock()
    risk = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       clock=clock, spend_policy=SpendPolicy(policy_version="t"))
    ws = WalletStore(":memory:")
    owner_auth = FakeOwnerAuth(clock, mfa=mfa)
    approvals = FakeApprovals(risk.store, ws)
    lifecycle = FakeLifecycle(risk.store, ws)
    provider, sink = FakeProvider(), FakeSink()
    deps = PlatformDeps(owner_authenticator=owner_auth, wallet_store=ws,
                        approvals=approvals if configured else None,
                        lifecycle=lifecycle if configured else None,
                        provider=provider if configured else None,
                        event_sink=sink if configured else None,
                        limits=limits or PlatformLimits(),
                        require_mfa_for_approval=require_mfa,
                        market_checker=market_checker)
    legacy = TokenRegistry.model_validate({"tokens": [
        {"id": "legacy-agent", "role": "agent", "agent_id": "agent_demo_001",
         "sha256": _sha(LEGACY_AGENT)},
        {"id": "legacy-ops", "role": "operator", "sha256": _sha(LEGACY_OPS)}]})
    app = create_app(service=risk, settings=settings(**settings_kw), registry=legacy,
                     platform=deps)
    client = TestClient(app, client=("testclient", 50000))
    return World(client, app, risk, deps, clock, owner_auth, approvals, lifecycle, provider,
                 sink, ws)
