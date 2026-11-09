"""LOCAL SIMULATION of a payment provider. Not PayPal, not a sandbox API, not a network
client.

`LocalSandboxSimulator` makes no network calls, holds no credentials and moves no money.
It only mints a simulation reference prefixed `sim-` so that it can never be mistaken for a
real provider transaction id. The wallet records it as a payment ATTEMPT; the ledger entry
stays `pending` (shown as `attempted`). Nothing here, and nothing in agentpal.wallet or
agentpal.workforce, records a payment outcome: only a VERIFIED provider event, applied
through `agentpal.lifecycle.PaymentLifecycleService.apply_provider_event`, may complete an
entry. The simulator never emits events, so a simulated attempt ends in
`reconciliation_required` after its deadline (SEC-16), never in `completed`.
"""

from __future__ import annotations

import uuid

from ..spending import ApprovalTerms

SIMULATION_PREFIX = "sim-"


class LocalSandboxSimulator:
    provider = "local_simulation"
    environment = "sandbox"
    is_real_money = False

    def submit(self, terms: ApprovalTerms) -> str:
        """Pretend to submit `terms`; return a simulation reference. No I/O."""
        if not isinstance(terms, ApprovalTerms):
            raise TypeError("a simulation needs the exact approved terms")
        return SIMULATION_PREFIX + uuid.uuid4().hex
