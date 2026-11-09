"""Strict approvals and injected cross-database interruptions never create authority."""
from datetime import timedelta
import sqlite3

import pytest

from agentpal.lifecycle.recovery import ConsistencyError, check_consistency, startup_recovery
from agentpal.spending import InvalidLedgerTransition, LedgerState
from test_lifecycle_recovery import ledger_request


@pytest.mark.parametrize("corrupt", ["missing", "owner", "digest", "expiry", "consumed", "session", "method"])
def test_strict_approved_entries_require_matching_unconsumed_approval(corrupt):
    risk, clock, request, terms = ledger_request()
    store = risk.store
    store.strict_execution = True
    store.approve_authenticated(request.request_id, terms, agent_id=request.agent_id,
        owner_id="o", session_id="session_recovery", auth_method="owner_api_token", now=clock())
    with store._tx() as c:
        c.execute("DROP TRIGGER trg_approvals_single_use")
        if corrupt == "missing":
            c.execute("DROP TRIGGER trg_approvals_no_delete")
            c.execute("DELETE FROM approvals")
        else:
            column, value = {"owner": ("owner_id", "other"), "digest": ("terms_sha256", "0"*64),
                "expiry": ("expires_at", (clock()+timedelta(days=1)).isoformat()),
                "consumed": ("consumed_at", clock().isoformat()), "session": ("session_id", ""),
                "method": ("auth_method", "untrusted")}[corrupt]
            c.execute(f"UPDATE approvals SET {column}=?", (value,))
    assert "approved_without_usable_approval" in {issue.kind for issue in check_consistency(store)}
    with pytest.raises(ConsistencyError):
        startup_recovery(store, now=clock(), recover_inflight=False)
    assert store.ledger_state(request.request_id) == LedgerState.approved


def test_failed_wallet_projection_commit_blocks_restart_without_releasing_hold(tmp_path):
    from agentpal.wallet import AgentRegistry, PermissionChecker, WalletService, WalletStore
    from test_sec_v2_isolation import make, req
    from agentpal.wallet.principals import OwnerPrincipal
    from agentpal.wallet.models import PaymentDraft
    from agentpal.workforce.roles import Role
    from agentpal.contracts import Capability
    ledger_path, wallet_path = tmp_path / "ledger.db", tmp_path / "wallet.db"
    risk, wallets = make(ledger_path), WalletStore(wallet_path)
    registry = AgentRegistry(wallets, clock=risk.clock)
    owner = OwnerPrincipal(owner_id="owner_a", token_id="test-operator")
    registry.register_owner(owner)
    registry.register_agent(owner, "agent_a", Role.payment)
    registry.grant(owner, "agent_a", Capability.payment_prepare)
    wallet = WalletService(registry, PermissionChecker(registry), risk,
                           id_factory=lambda: "pay_injected_gap")
    with wallets.tx() as c:
        c.execute("CREATE TRIGGER fail_projection BEFORE INSERT ON wallet_payment_requests "
                  "BEGIN SELECT RAISE(ABORT,'injected projection failure'); END")
    request = req("agent_a")
    draft = PaymentDraft(amount=request.amount, currency=request.currency,
                         merchant_name=request.merchant_name, purpose=request.purpose)
    with pytest.raises(sqlite3.IntegrityError):
        wallet.prepare_payment(registry.context_for("agent_a"), draft)
    assert risk.store.ledger_state("pay_injected_gap", agent_id="agent_a") == LedgerState.reserved
    risk.store.close()
    wallets.close()
    risk, wallets = make(ledger_path), WalletStore(wallet_path)
    with pytest.raises(ConsistencyError) as failure:
        startup_recovery(risk.store, wallets, now=risk.clock())
    assert "ledger_hold_without_wallet_request" in {i.kind for i in failure.value.report.issues}
    assert risk.store.ledger_state("pay_injected_gap", agent_id="agent_a") == LedgerState.reserved
    risk.store.close()
    wallets.close()


def test_wallet_decision_audit_failure_cannot_undo_or_duplicate_ledger_approval():
    from test_sec_v2_approvals import world
    w, _, owner, _, payment = world()
    with w.wallet_store.tx() as c:
        c.execute("CREATE TRIGGER fail_decision BEFORE INSERT ON wallet_owner_decisions "
                  "BEGIN SELECT RAISE(ABORT,'injected audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        w.wallet.approve(owner, payment.request_id, terms_sha256=payment.terms_sha256)
    assert w.risk.store.ledger_state(payment.request_id) == LedgerState.approved
    [approval] = w.risk.store.approvals(payment.request_id)
    assert approval["owner_id"] == owner.owner_id and approval["consumed_at"] is None
    assert check_consistency(w.risk.store, w.wallet_store) == []
    with w.wallet_store.tx() as c:
        c.execute("DROP TRIGGER fail_decision")
    with pytest.raises(InvalidLedgerTransition):
        w.wallet.approve(owner, payment.request_id, payment.terms)
    assert len(w.risk.store.approvals(payment.request_id)) == 1
