"""Composition of the real WS1 services with the /v1 protocols.

Lock order: preparation holds WalletStore.tx before entering AssessmentStore. Never
hold a ledger transaction while acquiring a wallet/identity lock. Owner/session reads,
ledger decisions, and wallet audit writes complete separately, in that order. Startup
consistency checks run before requests; the prebuilt WalletService avoids running a
cross-store check inside PaymentDesk's preparation transaction. Provider I/O happens
after the durable ledger transaction commits. Use separate files for all three stores.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

from ..analyzers import build_analyzer_chain
from ..config import Settings, get_settings, load_policy
from ..identity import OwnerIdentityService
from ..identity.store import IdentityStore
from ..infra_contracts import (
    AuthenticatedOwner, PaymentLifecycle, PaymentProvider, ProviderError, ProviderOrderRef, ProviderStatus,
)
from ..lifecycle import PaymentLifecycleService
from ..lifecycle.recovery import ConsistencyError, ConsistencyReport, check_consistency
from ..providers.config import load_config_from_env
from ..providers.paypal_sandbox import PayPalOrderEvent, PayPalSandboxProvider
from ..service import RiskService
from ..spending import ApprovalTerms, load_spend_policy
from ..store import AssessmentStore
from ..wallet.permissions import PermissionChecker
from ..wallet.registry import AgentRegistry
from ..wallet.sandbox import LocalSandboxSimulator
from ..wallet.service import WalletService
from ..wallet.store import WalletStore
from .deps import PlatformDeps, PlatformLimits
from .payments import take_pending_request_id


class WalletApprovalGateway:
    def __init__(self, wallet: WalletService):
        self.wallet = wallet

    def approve(self, owner: AuthenticatedOwner, request_id: str, *, terms_sha256: str):
        return self.wallet.approve(owner, request_id, terms_sha256=terms_sha256)

    def decline(self, owner: AuthenticatedOwner, request_id: str):
        return self.wallet.decline(owner, request_id)


class LocalSandboxProvider:
    """Simulator adapter. It cannot verify events and never reports completion."""

    name = LocalSandboxSimulator.provider
    environment = "sandbox"

    def __init__(self):
        self.simulator = LocalSandboxSimulator()

    def create_order(self, terms: ApprovalTerms, *, idempotency_key: str,
                     return_url: str | None = None) -> ProviderOrderRef:
        return ProviderOrderRef(provider=self.name, environment="sandbox",
            provider_order_id=self.simulator.submit(terms), idempotency_key=idempotency_key,
            status=ProviderStatus.pending)

    def capture_order(self, provider_order_id: str, *, idempotency_key: str):
        raise ProviderError("local simulator cannot verify capture", outcome_unknown=True)

    def get_order_status(self, provider_order_id: str):
        raise ProviderError("local simulator cannot verify status", outcome_unknown=True)

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes):
        raise ProviderError("local simulator cannot verify webhooks", outcome_unknown=True)


class _ObservedRead:
    """Delegate the existing lifecycle call unchanged; retain its GET observation."""
    def __init__(self, provider):
        self.provider = provider
        self.event = None

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def get_order_status(self, order_id):
        self.event = self.provider.get_order_status(order_id)
        return self.event


class WalletLifecycleGateway:
    def __init__(self, lifecycle: PaymentLifecycleService, wallets: WalletStore,
                 provider: PaymentProvider, resolver=None):
        self.resolver = resolver
        self.lifecycle = lifecycle
        self.wallets = wallets
        self.provider = provider

    def _agent(self, request_id: str) -> str:
        # Request ids in /v1 are server-generated. Resolve their agent from trusted
        # platform metadata, and require a matching ledger request; never take a caller id.
        rows = self.wallets.query("SELECT agent_id FROM platform_payment_meta WHERE request_id=?",
                                   (request_id,))
        if len(rows) != 1:
            raise LookupError("unknown platform request")
        agent_id = rows[0]["agent_id"]
        if self.lifecycle.store.request_status(request_id, agent_id=agent_id) is None:
            raise LookupError("platform/ledger request mismatch")
        return agent_id

    def status(self, request_id: str, *, agent_id: str | None = None):
        return self.lifecycle.status(request_id, agent_id=agent_id)

    def begin_execution(self, agent_id: str, request_id: str, terms: ApprovalTerms,
                        provider: PaymentProvider):
        return self.lifecycle.begin_execution(agent_id, request_id, terms, provider.name)

    def submit_to_provider(self, ticket, provider: PaymentProvider):
        return self.lifecycle.submit_to_provider(ticket, provider)

    def _provider_for(self, agent_id):
        if self.resolver is None:
            return self.provider
        rows = self.wallets.query("SELECT owner_id FROM wallet_agents WHERE agent_id=?", (agent_id,))
        if len(rows) != 1:
            raise LookupError("unknown agent")
        owner_id = rows[0]["owner_id"]
        if self.resolver.integrations.fields(owner_id, "paypal_sandbox"):
            return self.resolver.paypal(owner_id)
        return self.provider

    def reconcile(self, request_id: str):
        agent_id = self._agent(request_id)
        provider = self._provider_for(agent_id)
        observed = _ObservedRead(provider)
        result = self.lifecycle.reconcile(agent_id, request_id, observed)
        event = observed.event
        # This receipt grants no financial authority. Record only an exact authenticated
        # PayPal GET agreeing with the settled ledger, after the lifecycle has handled it.
        if (isinstance(provider, PayPalSandboxProvider)
                and isinstance(event, PayPalOrderEvent) and event.verified is True
                and event.provider == "paypal" and event.environment == "sandbox"
                and event.event_type == "ORDER_STATUS_READ"
                and event.status.value in {"completed", "declined", "refunded", "reversed"}
                and self.lifecycle.paypal.matches(agent_id, request_id, event)):
            entry = self.lifecycle.store.ledger_entry(request_id, agent_id=agent_id)
            target = {"completed": "completed", "declined": "failed",
                      "refunded": "reversed", "reversed": "reversed"}[event.status.value]
            if entry and entry["state"] == target:
                with self.wallets.tx() as c:
                    c.execute("INSERT OR IGNORE INTO platform_paypal_read_receipts "
                              "(agent_id,request_id,event_id,at) VALUES (?,?,?,?)",
                              (agent_id, request_id, event.event_id,
                               self.lifecycle.clock().isoformat()))
        return result

    def capture(self, request_id: str):
        agent_id = self._agent(request_id)
        provider = self._provider_for(agent_id)
        if self.resolver is not None:
            row = self.wallets.query("SELECT owner_id,terms_json FROM wallet_payment_requests "
                                     "WHERE agent_id=? AND request_id=?", (agent_id, request_id))[0]
            terms = ApprovalTerms.model_validate_json(row["terms_json"])
            self.resolver.integrations.resources.check_amount(row["owner_id"], terms.amount,
                                                              server_provided=provider is self.provider)
        result = self.lifecycle.capture(agent_id, request_id, provider)
        if result == PaymentLifecycle.provider_confirmed and isinstance(provider, PayPalSandboxProvider):
            try:
                # A separate authenticated final GET, using reconcile's exact-term and
                # accepted-state receipt gates. The capture POST itself is never proof.
                # Provider HTTP already bounds response size, connect and read timeouts.
                self.reconcile(request_id)
            except Exception:
                # Capture has already completed. A failed optional read cannot undo it
                # or make callers retry the financial operation. No receipt is invented.
                pass
        return result

    def provider_references(self, request_id: str) -> list[ProviderOrderRef]:
        row = self.lifecycle.store.provider_order(request_id, agent_id=self._agent(request_id))
        if row is None or not row["provider_order_id"]:
            return []
        return [ProviderOrderRef(provider=row["provider"], environment="sandbox",
            provider_order_id=row["provider_order_id"], idempotency_key=row["create_key"],
            status=ProviderStatus(row["last_status"]), payer_action_url=(
                self.lifecycle.store.provider_payer_url(request_id, agent_id=row["agent_id"])))]


@dataclass
class WiredPlatformDeps(PlatformDeps):
    """PlatformDeps plus the risk runtime used by create_app and explicit cleanup."""

    risk_service: RiskService | None = None

    def close(self) -> None:
        if self.provider_resolver:
            self.provider_resolver.close()
        if isinstance(self.provider, PayPalSandboxProvider):
            self.provider.http_client.close()
        self.wallet_store.close()
        self.owner_authenticator.store.close()
        self.risk_service.store.close()


def _market_checker(env: Mapping[str, str]):
    """Channel3 market check (sponsor integration): enabled iff CHANNEL3_API_KEY is set."""
    key = (env.get("CHANNEL3_API_KEY") or "").strip()
    if not key:
        return None
    from ..intelligence.market_check import MarketChecker
    from ..tools.channel3 import Channel3Client
    return MarketChecker(Channel3Client(key))


def _kernel_tools(env: Mapping[str, str]):
    """Optional sponsor components; K1/K2 may not have been integrated yet."""
    key = (env.get("KERNEL_API_KEY") or "").strip()
    if not key:
        return None
    try:
        from ..tools.kernel_browser import KernelBrowserProvider
        from ..intelligence.page_check import KernelPageChecker
        browser = KernelBrowserProvider(api_key=key)
        return browser, KernelPageChecker(browser)
    except Exception:
        # Optional advisory tooling must never prevent the financial platform starting.
        # Do not log construction errors: they may include provider configuration.
        return None


def build_platform(*, settings: Settings | None = None,
                   identity_db_path: str | Path | None = None,
                   wallet_db_path: str | Path | None = None,
                   environ: Mapping[str, str] | None = None,
                   limits: PlatformLimits | None = None) -> WiredPlatformDeps:
    """Build strict /v1 dependencies; default DB paths are siblings of the risk DB.

    AGENTPAL_IDENTITY_DB_PATH / AGENTPAL_WALLET_DB_PATH override those defaults.
    Any nonempty PAYPAL_SANDBOX_* setting opts into PayPal configuration validation;
    partial credentials fail closed instead of silently selecting the simulator.
    """
    settings = settings or get_settings()
    env = os.environ if environ is None else environ
    risk_path = settings.db_path

    def sibling(suffix):
        return ":memory:" if risk_path == ":memory:" else risk_path + suffix

    identity_path = str(identity_db_path or env.get("AGENTPAL_IDENTITY_DB_PATH")
                        or sibling(".identity.db"))
    wallet_path = str(wallet_db_path or env.get("AGENTPAL_WALLET_DB_PATH")
                      or sibling(".wallet.db"))
    paths = [Path(p).resolve() for p in (risk_path, identity_path, wallet_path) if p != ":memory:"]
    if len(paths) != len(set(paths)) or any(
            a.exists() and b.exists() and a.samefile(b)
            for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("identity, wallet and ledger require separate database files")
    provider = (PayPalSandboxProvider(load_config_from_env(env))
                if any(value for key, value in env.items() if key.startswith("PAYPAL_SANDBOX_"))
                else LocalSandboxProvider())
    data_dir = (Path(env.get("AGENTPAL_DATA_DIR") or Path(risk_path).resolve().parent)
                if risk_path != ":memory:" else None)
    if data_dir:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = AssessmentStore(risk_path, strict_execution=True)
    wallets = WalletStore(wallet_path)
    identity = OwnerIdentityService(IdentityStore(identity_path))
    try:
        risk = RiskService(load_policy(settings.policy_path), store, build_analyzer_chain(settings),
            spend_policy=load_spend_policy(settings.spend_policy_path),
            model_timeout_s=settings.model_timeout_seconds, recovery_wallet_store=wallets)
        issues = check_consistency(store, wallets)
        if issues:
            raise ConsistencyError(ConsistencyReport(issues=issues))
        registry = AgentRegistry(wallets, clock=risk.clock)
        wallet = WalletService(registry, PermissionChecker(registry, clock=risk.clock), risk,
            identity=identity, require_authenticated_owner=True, id_factory=take_pending_request_id)
        lifecycle = PaymentLifecycleService(store, paypal_provider=provider)

        def wallet_factory(registry, checker, risk_service, id_factory):
            if risk_service is not risk or registry.store is not wallets:
                raise ValueError("platform must use its wired risk service and wallet store")
            return wallet

        from .integrations import Integrations
        from .resolution import ProviderResolver
        from .secrets import SecretVault
        from .setup import SetupService
        mode = env.get("AGENTPAL_MODE", "hosted" if settings.hosted_proxy == "render" else "self_hosted")
        if mode not in {"hosted", "self_hosted"}:
            raise ValueError("AGENTPAL_MODE must be self_hosted or hosted")
        if settings.hosted_proxy == "render" and mode != "hosted":
            raise ValueError("hosted proxy requires hosted workspace mode")
        rate_path = ":memory:" if risk_path == ":memory:" else risk_path + ".workspace-limits.db"
        vault = SecretVault.configured(env, data_dir, mode == "hosted")
        integrations = Integrations(wallets, vault, env, rate_path, mode == "hosted")
        setup = SetupService(identity, env, data_dir, mode == "hosted", rate_path)
        resolver = ProviderResolver(integrations, provider, _market_checker, _kernel_tools)
        kernel_tools = _kernel_tools(env)
        return WiredPlatformDeps(owner_authenticator=identity, wallet_store=wallets,
            approvals=WalletApprovalGateway(wallet),
            lifecycle=WalletLifecycleGateway(lifecycle, wallets, provider, resolver), provider=provider,
            setup=setup, integrations=integrations, provider_resolver=resolver,
            event_sink=lifecycle, limits=limits or PlatformLimits(),
            require_mfa_for_approval=settings.require_mfa_for_approval,
            market_checker=_market_checker(env),
            browser=kernel_tools[0] if kernel_tools else None,
            page_checker=kernel_tools[1] if kernel_tools else None,
            wallet_service_factory=wallet_factory, risk_service=risk)
    except BaseException:
        identity.store.close()
        wallets.close()
        store.close()
        if isinstance(provider, PayPalSandboxProvider):
            provider.http_client.close()
        raise
