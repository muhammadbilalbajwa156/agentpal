"""`WalletBackedSpendPolicy`: owner-configured wallets as the authorized administration
service for the budgets of PLATFORM agents (lead decision D1).

`spending.py` says budgets come only from the operator's file "or, later, an authorized
administration service". This is that service, for agents registered through /v1 only:

* a platform agent's limits are exactly its ACTIVE wallets' limits, which an authenticated
  owner set within the operator's hard caps (`PlatformLimits`);
* a platform agent with no active wallet in a currency has no limits in it, so the
  deterministic reservation blocks (NO_SPENDING_POLICY / fail closed). A frozen wallet is
  treated as missing;
* every other agent is answered from the operator spend-policy file, unchanged;
* nothing a request carries can change any of this.

It is installed on `RiskService.spend_policy` (an attribute), so neither `spending.py` nor
`service.py` is modified.
"""

from __future__ import annotations

from pydantic import PrivateAttr

from ..spending import AgentSpendPolicy, SpendPolicy
from .wallets import WalletBook


class WalletBackedSpendPolicy(SpendPolicy):
    _wallets: WalletBook | None = PrivateAttr(default=None)
    _owner_of: object = PrivateAttr(default=None)

    @classmethod
    def wrap(cls, base: SpendPolicy, wallets: WalletBook, owner_of) -> "WalletBackedSpendPolicy":
        policy = cls.model_validate(base.model_dump())
        policy._wallets = wallets
        policy._owner_of = owner_of
        return policy

    def _platform(self, agent_id: str) -> bool:
        return self._wallets is not None and self._wallets.is_platform_agent(agent_id)

    def for_agent(self, agent_id: str) -> AgentSpendPolicy | None:
        if not self._platform(agent_id):
            return super().for_agent(agent_id)
        owner_id = self._owner_of(agent_id)
        currencies = self._wallets.active_limits(agent_id)
        if owner_id is None or not currencies:
            return None
        return AgentSpendPolicy(owner_id=owner_id, currencies=currencies)

    def uses_demo_default(self, agent_id: str) -> bool:
        if self._platform(agent_id):
            return False
        return super().uses_demo_default(agent_id)


def install(risk_service, wallets: WalletBook, owner_of) -> None:
    current = risk_service.spend_policy
    if isinstance(current, WalletBackedSpendPolicy):
        return
    risk_service.spend_policy = WalletBackedSpendPolicy.wrap(current, wallets, owner_of)
