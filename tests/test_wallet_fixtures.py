"""Shared, offline fixtures for the wallet / workforce / tools tests (imported by them).

A fully real stack: the operator spend policy, the real RiskService (rules + heuristic
analyzer, no model), the real AssessmentStore ledger (in memory) and a WalletStore."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy
from agentpal.store import AssessmentStore
from agentpal.wallet import (
    AgentRegistry,
    OwnerPrincipal,
    PaymentDraft,
    PermissionChecker,
    WalletService,
    WalletStore,
)
from agentpal.workforce.roles import Role

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

USD = {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
       "per_transaction_hard_limit": "5000.00", "per_transaction_review_limit": "1000.00"}


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@dataclass
class World:
    clock: Clock
    risk: RiskService
    wallet_store: WalletStore
    registry: AgentRegistry
    checker: PermissionChecker
    wallet: WalletService
    alice: OwnerPrincipal
    bob: OwnerPrincipal
    agents: dict[str, str] = field(default_factory=dict)  # role -> alice's agent id

    def ctx(self, agent_id: str):
        return self.registry.context_for(agent_id)


def spend_policy(extra_agents: dict | None = None) -> SpendPolicy:
    agents = {
        "alice_payment": {"owner_id": "owner_alice", "currencies": {"USD": USD}},
        "alice_finance": {"owner_id": "owner_alice", "currencies": {"USD": USD}},
        "bob_payment": {"owner_id": "owner_bob", "currencies": {"USD": USD}},
        "mismatched_agent": {"owner_id": "owner_bob", "currencies": {"USD": USD}},
    }
    agents.update(extra_agents or {})
    return SpendPolicy.model_validate({"policy_version": "wallet-test",
                                       "configuration": "operator", "agents": agents})


def build_world(sp: SpendPolicy | None = None) -> World:
    clock = Clock()
    risk = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                       clock=clock, spend_policy=sp or spend_policy())
    ws = WalletStore(":memory:")
    registry = AgentRegistry(ws, clock=clock)
    checker = PermissionChecker(registry)
    wallet = WalletService(registry, checker, risk)
    alice = OwnerPrincipal(owner_id="owner_alice", token_id="ops-alice")
    bob = OwnerPrincipal(owner_id="owner_bob", token_id="ops-bob")
    registry.register_owner(alice, "Alice")
    registry.register_owner(bob, "Bob")
    world = World(clock, risk, ws, registry, checker, wallet, alice, bob)
    for role in Role:
        agent_id = f"alice_{role.value}"
        registry.register_agent(alice, agent_id, role)
        world.agents[role.value] = agent_id
    registry.register_agent(bob, "bob_payment", Role.payment)
    registry.register_agent(alice, "mismatched_agent", Role.payment)  # policy says bob
    return world


def draft(**change) -> PaymentDraft:
    base = dict(amount=Decimal("49.00"), currency="USD", merchant_name="Example Hosting",
                merchant_domain="example.com", purpose="Monthly hosting subscription",
                business_justification="Hosting for a customer project", recurring=True,
                billing_interval="monthly")
    base.update(change)
    return PaymentDraft(**base)


def test_fixture_world_builds():
    w = build_world()
    assert set(w.agents) == {r.value for r in Role}
    assert w.registry.get_agent("bob_payment").owner_id == "owner_bob"
