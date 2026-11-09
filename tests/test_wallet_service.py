"""WalletService on the real spend policy, risk engine and ledger.

* balances are derived from ledger states (no free-floating number);
* no AI agent can approve its own (or any) payment;
* approval binds exact terms; changed terms are refused;
* sandbox execution is a LOCAL SIMULATION that ends `attempted`, never `completed`;
* nothing in wallet/ or workforce/ records a payment outcome."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from agentpal.auth import Principal
from agentpal.contracts import Capability
from agentpal.spending import (
    ApprovalTerms,
    ApprovalTermsMismatch,
    InvalidLedgerTransition,
    LedgerState,
)
from agentpal.wallet import (
    InstrumentKind,
    NotAuthorized,
    PaymentInstrumentRef,
    PermissionDenied,
    WalletRefused,
)

from test_wallet_fixtures import build_world, draft

PKG = Path(__file__).resolve().parent.parent / "agentpal"


def prepared(w, **change):
    return w.wallet.prepare_payment(w.ctx("alice_payment"), draft(**change))


def changed(terms: ApprovalTerms, **change) -> ApprovalTerms:
    return ApprovalTerms.model_validate({**terms.model_dump(), **change})


# ------------------------------------------------------------------ allowance
def test_allowance_is_derived_from_ledger_states():
    w = build_world()
    ctx = w.ctx("alice_payment")
    usd = w.wallet.view(ctx).allowances[0]
    assert (usd.monthly_budget, usd.committed, usd.available) == (
        Decimal("100.00"), 0, Decimal("100.00"))
    p = prepared(w)
    assert p.ledger_state == LedgerState.reserved
    usd = w.wallet.view(ctx).allowances[0]
    assert usd.reserved == Decimal("49.00") and usd.available == Decimal("51.00")
    w.wallet.approve(w.alice, p.request_id, p.terms)
    w.wallet.execute_sandbox(ctx, p.request_id, p.terms)
    usd = w.wallet.view(ctx).allowances[0]
    b = w.risk.store.spend_breakdown("alice_payment", "USD", w.clock())
    assert usd.attempted == b["attempted"] == Decimal("49.00")
    assert usd.completed == b["completed"] == 0
    assert usd.committed == b["committed"] and usd.available == Decimal("51.00")
    assert "not money" in usd.label and w.wallet.view(ctx).is_real_money is False


def test_declined_request_releases_the_hold():
    w = build_world()
    p = prepared(w)
    w.wallet.decline(w.alice, p.request_id)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.released
    assert w.wallet.view(w.ctx("alice_payment")).allowances[0].available == Decimal("100.00")


def test_operator_policy_must_list_the_agent_and_agree_on_the_owner():
    w = build_world()
    # alice_payment is listed; mismatched_agent is listed under owner_bob in the policy
    v = w.wallet.view(w.ctx("mismatched_agent"))
    assert v.refusal == "OWNER_MISMATCH" and v.allowances == []
    with pytest.raises(WalletRefused, match="OWNER_MISMATCH"):
        w.wallet.prepare_payment(w.ctx("mismatched_agent"), draft())
    # an agent with no spend policy in an operator configuration has no allowance
    w.registry.grant(w.alice, "alice_research", Capability.wallet_read)
    assert w.wallet.view(w.ctx("alice_research")).refusal == "NO_SPENDING_POLICY"


def test_over_budget_request_gets_no_hold_and_cannot_be_approved():
    w = build_world()
    p = prepared(w, amount=Decimal("150.00"))
    assert p.ledger_state is None and p.recommendation == "block"
    with pytest.raises(WalletRefused, match="NO_HOLD"):
        w.wallet.approve(w.alice, p.request_id, p.terms)


def test_draft_cannot_raise_the_budget():
    w = build_world()
    p = prepared(w)
    usd = w.wallet.view(w.ctx("alice_payment")).allowances[0]
    assert usd.monthly_budget == Decimal("100.00")  # operator value only
    assert p.terms.agent_id == "alice_payment"


# ---------------------------------------------------------- capabilities
def test_preparing_and_reading_need_capabilities():
    w = build_world()
    for agent in ("alice_research", "alice_shopping", "alice_finance",
                  "alice_communication", "alice_verification"):
        with pytest.raises(PermissionDenied, match="NOT_GRANTED"):
            w.wallet.prepare_payment(w.ctx(agent), draft())
    with pytest.raises(PermissionDenied):
        w.wallet.view(w.ctx("alice_communication"))


def test_finance_monitor_is_owner_scoped():
    w = build_world()
    prepared(w)
    fin = w.ctx("alice_finance")
    assert w.wallet.finance_view(fin, "alice_payment").allowances[0].reserved == \
        Decimal("49.00")
    assert len(w.wallet.history(fin, "alice_payment")) == 1
    with pytest.raises(PermissionDenied, match="CROSS_OWNER"):
        w.wallet.finance_view(fin, "bob_payment")
    with pytest.raises(PermissionDenied, match="CROSS_OWNER"):  # no existence oracle
        w.wallet.finance_view(fin, "no_such_agent")
    with pytest.raises(PermissionDenied, match="NOT_GRANTED"):
        w.wallet.history(w.ctx("alice_payment"), "alice_finance")


def test_history_shows_the_ledger_trail():
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    (rec,) = w.wallet.history(w.ctx("alice_payment"))
    assert rec.state == LedgerState.approved
    assert [e.to_state for e in rec.events] == ["reserved", "approved"]
    assert rec.events[1].actor == "owner:owner_alice|token:ops-alice"


# ---------------------------------------------------- separation of duties
@pytest.mark.parametrize("agent", ["alice_payment", "alice_shopping", "alice_finance",
                                   "alice_research", "alice_verification",
                                   "alice_communication"])
def test_no_agent_can_approve_any_payment(agent):
    w = build_world()
    p = prepared(w)
    with pytest.raises(NotAuthorized, match="SEPARATION_OF_DUTIES"):
        w.wallet.approve(w.ctx(agent), p.request_id, p.terms)
    with pytest.raises(NotAuthorized, match="SEPARATION_OF_DUTIES"):
        w.wallet.decline(w.ctx(agent), p.request_id)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved


def test_an_agent_token_principal_cannot_approve():
    w = build_world()
    p = prepared(w)
    agent_principal = Principal(token_id="agent-key-1", role="agent",
                                agent_id="alice_payment")
    with pytest.raises(NotAuthorized):
        w.wallet.approve(agent_principal, p.request_id, p.terms)
    with pytest.raises(NotAuthorized):
        w.wallet.pending_approvals(w.ctx("alice_payment"))


def test_another_owner_cannot_approve_and_the_refusal_is_audited():
    w = build_world()
    p = prepared(w)
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.wallet.approve(w.bob, p.request_id, p.terms)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved
    (row,) = w.wallet.owner_decisions(p.request_id)
    assert row["decision"] == "refused" and row["owner_id"] == "owner_bob"


@pytest.mark.parametrize("change", [{"amount": Decimal("49.01")}, {"currency": "GBP"},
                                    {"merchant_name": "Example Hosting Ltd"},
                                    {"merchant_domain": "examp1e.com"},
                                    {"purpose": "Gift cards"},
                                    {"agent_id": "bob_payment"}])
def test_approval_with_changed_terms_is_refused(change):
    w = build_world()
    p = prepared(w)
    with pytest.raises(ApprovalTermsMismatch):
        w.wallet.approve(w.alice, p.request_id, changed(p.terms, **change))
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved
    assert w.wallet.owner_decisions(p.request_id)[-1]["decision"] == "refused"


def test_pending_approvals_shows_the_owner_exact_terms():
    w = build_world()
    p = prepared(w)
    (shown,) = w.wallet.pending_approvals(w.alice)
    assert shown.terms == p.terms and shown.terms_sha256 == p.terms.digest()
    assert w.wallet.pending_approvals(w.bob) == []


# --------------------------------------------------------- local simulation
def test_sandbox_execution_is_a_local_simulation_and_never_completes():
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    ex = w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)
    assert ex.simulation_reference.startswith("sim-") and ex.provider == "local_simulation"
    assert ex.ledger_state == LedgerState.pending and ex.is_real_money is False
    assert w.risk.store.spend_breakdown("alice_payment", "USD", w.clock())["completed"] == 0
    with pytest.raises(WalletRefused):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)


def test_execution_requires_an_owner_approval():
    w = build_world()
    p = prepared(w)
    with pytest.raises(WalletRefused, match="NO_OWNER_APPROVAL"):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved


def test_an_approval_that_bypassed_the_wallet_owner_check_is_not_enough():
    """SEC-15: the store's decided_by is free text; the wallet only executes approvals it
    recorded for the authenticated owner of the entry."""
    w = build_world()
    p = prepared(w)
    w.risk.store.record_owner_decision(p.request_id, "approved", "alice_payment",
                                       terms=p.terms)
    with pytest.raises(WalletRefused, match="NO_OWNER_APPROVAL"):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.approved


@pytest.mark.parametrize("change", [{"amount": Decimal("50.00")},
                                    {"merchant_domain": "evil.example"}])
def test_execution_with_changed_terms_is_refused(change):
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    with pytest.raises(WalletRefused, match="TERMS_MISMATCH"):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id,
                                 changed(p.terms, **change))
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.approved


def test_execution_only_by_the_requesting_payment_agent():
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    with pytest.raises(PermissionDenied, match="NOT_GRANTED"):
        w.wallet.execute_sandbox(w.ctx("alice_finance"), p.request_id, p.terms)
    with pytest.raises(PermissionDenied, match="CROSS_OWNER"):
        w.wallet.execute_sandbox(w.ctx("bob_payment"), p.request_id, p.terms)
    w.registry.register_agent(w.alice, "alice_payment2", "payment")
    with pytest.raises(PermissionDenied, match="OTHER_AGENT"):
        w.wallet.execute_sandbox(w.ctx("alice_payment2"), p.request_id, p.terms)


def test_expired_approval_cannot_be_executed():
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    w.clock.advance(minutes=16)
    with pytest.raises(InvalidLedgerTransition):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.released


def test_revoked_execute_capability_stops_execution():
    w = build_world()
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    w.registry.revoke(w.alice, "alice_payment", Capability.payment_execute_sandbox)
    with pytest.raises(PermissionDenied, match="NOT_GRANTED"):
        w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)


# ------------------------------------------------- outcomes are never recorded
def _calls_record_payment_outcome(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr == "record_payment_outcome")
            or (isinstance(n, ast.Name) and n.id == "record_payment_outcome")
            or (isinstance(n, ast.Constant) and n.value == "record_payment_outcome")]


def test_no_wallet_or_workforce_code_records_a_payment_outcome():
    files = sorted((PKG / "wallet").rglob("*.py")) + sorted((PKG / "workforce").rglob("*.py"))
    assert len(files) >= 5
    offenders = {str(f): lines for f in files if (lines := _calls_record_payment_outcome(f))}
    assert offenders == {}


def test_full_flow_never_reaches_record_payment_outcome(monkeypatch):
    w = build_world()

    def forbidden(*a, **k):
        raise AssertionError("wallet must never record a payment outcome")

    monkeypatch.setattr(w.risk.store, "record_payment_outcome", forbidden)
    p = prepared(w)
    w.wallet.approve(w.alice, p.request_id, p.terms)
    w.wallet.execute_sandbox(w.ctx("alice_payment"), p.request_id, p.terms)
    w.wallet.history(w.ctx("alice_payment"))
    w.wallet.view(w.ctx("alice_payment"))


# ---------------------------------------------------------------- instruments
def test_instruments_are_owner_scoped_references():
    w = build_world()
    ref = PaymentInstrumentRef(instrument_id="alice_pp_sb", owner_id="owner_alice",
                               kind=InstrumentKind.provider_sandbox_instrument,
                               provider="paypal_sandbox", external_ref="sb-merchant-1")
    w.registry.add_instrument(w.alice, ref)
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.registry.add_instrument(w.bob, ref)
    with pytest.raises(NotAuthorized):
        w.registry.add_instrument(w.ctx("alice_payment"), ref)
    assert w.wallet.view(w.ctx("alice_payment")).instruments == [ref]
    assert w.wallet.view(w.ctx("bob_payment")).instruments == []
