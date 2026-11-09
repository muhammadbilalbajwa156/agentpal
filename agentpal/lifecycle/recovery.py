"""Startup recovery and cross-database consistency checks (SEC-22).

AgentPal keeps two SQLite databases: the risk/ledger database (`AssessmentStore`, the
AUTHORITY for holds, approvals, attempts, provider orders and outcomes) and the wallet
database (`WalletStore`, owned by the wallet registry: agents, grants, the owner-facing copy
of the terms, the wallet's decision log and simulation log). They are not updated
atomically together. Recovery therefore never "repairs" money state from the wallet side:
it applies the ledger's own fail-closed rules and REPORTS every cross-database mismatch.
Execution only ever trusts the ledger database (see docs/CONSISTENCY.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from ..spending import ApprovalTerms
from ..store import AssessmentStore, _as_utc
from .service import PaymentLifecycleService


class Inconsistency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str
    agent_id: str | None
    request_id: str
    detail: str = ""


class ConsistencyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    moved_to_reconciliation: list[tuple[str, str]] = Field(default_factory=list)
    stale_claims_recovered: list[str] = Field(default_factory=list)
    reconciliation_required: list[tuple[str, str]] = Field(default_factory=list)
    issues: list[Inconsistency] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.issues


class ConsistencyError(RuntimeError):
    """Startup is refused until an operator resolves the attached consistency report."""

    def __init__(self, report: ConsistencyReport):
        super().__init__("financial consistency check failed")
        self.report = report


def check_consistency(store: AssessmentStore, wallet_store=None) -> list[Inconsistency]:
    """Read-only comparison. `wallet_store` is anything with a read-only `query(sql,
    params)` (agentpal.wallet.store.WalletStore)."""
    import re

    issues: list[Inconsistency] = []
    # ledger-internal invariants
    for r in store.query(
            "SELECT l.agent_id, l.request_id, l.state FROM ledger_entries l WHERE l.state IN "
            "('pending','reconciliation_required','completed','failed','cancelled','reversed')"
            " AND NOT EXISTS (SELECT 1 FROM payment_attempts a WHERE a.agent_id=l.agent_id "
            "AND a.request_id=l.request_id)"):
        issues.append(Inconsistency(kind="settled_without_attempt", agent_id=r["agent_id"],
                                    request_id=r["request_id"], detail=r["state"]))
    for r in store.query(
            "SELECT a.agent_id, a.request_id FROM approvals a JOIN ledger_entries l ON "
            "l.agent_id=a.agent_id AND l.request_id=a.request_id WHERE a.consumed_at IS NOT "
            "NULL AND l.state IN ('reserved','approved')"):
        issues.append(Inconsistency(kind="consumed_approval_without_attempt",
                                    agent_id=r["agent_id"], request_id=r["request_id"]))
    if store.strict_execution:
        for r in store.query(
                "SELECT l.agent_id,l.request_id,l.owner_id AS ledger_owner,"
                "l.expires_at AS ledger_expiry,t.terms_sha256 AS expected_digest,"
                "q.effective_expires_at AS request_expiry,a.* FROM ledger_entries l "
                "LEFT JOIN approvals a ON a.agent_id=l.agent_id AND a.request_id=l.request_id "
                "LEFT JOIN request_terms t ON t.agent_id=l.agent_id AND t.request_id=l.request_id "
                "LEFT JOIN requests q ON q.agent_id=l.agent_id AND q.request_id=l.request_id "
                "WHERE l.state='approved'"):
            try:
                approved = _as_utc(datetime.fromisoformat(r["approved_at"]))
                expires = _as_utc(datetime.fromisoformat(r["expires_at"]))
                request_expiry = _as_utc(datetime.fromisoformat(r["request_expiry"]))
                ledger_expiry = _as_utc(datetime.fromisoformat(r["ledger_expiry"]))
                valid = (approved < expires <= request_expiry and expires == ledger_expiry
                         and r["owner_id"] == r["ledger_owner"] and r["owner_id"] is not None
                         and r["owner_id"] != r["agent_id"] and r["session_id"]
                         and r["auth_method"] in ("owner_api_token", "owner_session")
                         and r["terms_sha256"] == r["expected_digest"]
                         and r["expected_digest"] is not None
                         and r["consumed_at"] is None and r["consumed_by"] is None)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                issues.append(Inconsistency(kind="approved_without_usable_approval",
                                            agent_id=r["agent_id"], request_id=r["request_id"]))
        for r in store.query(
                "SELECT l.agent_id, l.request_id FROM ledger_entries l WHERE l.state IN "
                "('pending','reconciliation_required','completed','failed','cancelled','reversed') "
                "AND NOT EXISTS (SELECT 1 FROM provider_orders o WHERE "
                "o.agent_id=l.agent_id AND o.request_id=l.request_id)"):
            issues.append(Inconsistency(kind="attempt_without_provider_order",
                                        agent_id=r["agent_id"], request_id=r["request_id"]))
    for r in store.query(
            "SELECT o.agent_id, o.request_id FROM provider_orders o LEFT JOIN approvals a "
            "ON a.agent_id=o.agent_id AND a.request_id=o.request_id "
            "LEFT JOIN ledger_entries l ON l.agent_id=o.agent_id AND l.request_id=o.request_id "
            "WHERE a.approval_id IS NULL OR a.consumed_at IS NULL OR "
            "a.consumed_by IS NOT o.create_key OR l.request_id IS NULL OR l.state IN "
            "('reserved','approved','released')"):
        issues.append(Inconsistency(kind="provider_order_without_consumed_approval",
                                    agent_id=r["agent_id"], request_id=r["request_id"]))
    for r in store.query(
            "SELECT o.agent_id, o.request_id FROM provider_orders o WHERE NOT EXISTS "
            "(SELECT 1 FROM payment_attempts a WHERE a.agent_id=o.agent_id AND "
            "a.request_id=o.request_id AND a.provider=o.provider AND "
            "a.provider_reference=o.create_key)"):
        issues.append(Inconsistency(kind="provider_attempt_differs", agent_id=r["agent_id"],
                                    request_id=r["request_id"]))
    for r in store.query(
            "SELECT o.agent_id, o.request_id, a.owner_id AS approval_owner, "
            "l.owner_id AS ledger_owner, a.terms_sha256 AS approval_digest, "
            "t.terms_sha256 AS request_digest, a.approved_at, a.expires_at, a.consumed_at "
            "FROM provider_orders o LEFT JOIN approvals a ON a.agent_id=o.agent_id "
            "AND a.request_id=o.request_id LEFT JOIN ledger_entries l ON "
            "l.agent_id=o.agent_id AND l.request_id=o.request_id LEFT JOIN request_terms t "
            "ON t.agent_id=o.agent_id AND t.request_id=o.request_id"):
        try:
            approved = _as_utc(datetime.fromisoformat(r["approved_at"]))
            expires = _as_utc(datetime.fromisoformat(r["expires_at"]))
            consumed = _as_utc(datetime.fromisoformat(r["consumed_at"]))
            valid_window = approved <= consumed < expires
        except (TypeError, ValueError):
            valid_window = False
        if not valid_window or r["approval_owner"] is None \
                or r["approval_owner"] != r["ledger_owner"] \
                or r["approval_digest"] is None \
                or r["approval_digest"] != r["request_digest"]:
            issues.append(Inconsistency(kind="provider_approval_differs", agent_id=r["agent_id"],
                                        request_id=r["request_id"]))
    for r in store.query(
            "SELECT l.agent_id, l.request_id, l.amount AS ledger_amount, "
            "l.state AS ledger_state, l.owner_id AS ledger_owner, "
            "r.amount AS request_amount, l.currency AS ledger_currency, "
            "r.currency AS request_currency FROM ledger_entries l LEFT JOIN requests r "
            "ON r.agent_id=l.agent_id AND r.request_id=l.request_id"):
        try:
            ledger_amount = Decimal(r["ledger_amount"])
            request_amount = Decimal(r["request_amount"])
            amounts_match = (ledger_amount.is_finite() and request_amount.is_finite()
                             and ledger_amount == request_amount)
            if r["ledger_state"] == "completed":
                # Only verified refund credits may explain net completed spending.
                # Include either marker identity so a forged actor/reason is refused,
                # and bound reads one past the maximum 100 refund claims.
                credits = store.query(
                    "SELECT actor, reason FROM ledger_events WHERE agent_id=? AND "
                    "request_id=? AND (actor='verified:paypal_refund' OR "
                    "reason GLOB 'paypal_refund_credit:*') ORDER BY id LIMIT 101",
                    (r["agent_id"], r["request_id"]))
                if credits or not amounts_match:
                    amounts_match = False
                    if wallet_store is not None and ledger_amount > 0 and len(credits) <= 100:
                        seen = set()
                        total = Decimal(0)
                        valid = True
                        for credit in credits:
                            match = re.fullmatch(
                                r"paypal_refund_credit:(rfd-[0-9a-f]{32}):"
                                r"([0-9]{1,10}(?:\.[0-9]{1,8})?)", credit["reason"] or "")
                            if credit["actor"] != "verified:paypal_refund" or match is None:
                                valid = False
                                break
                            refund_id, value = match.groups()
                            amount = Decimal(value)
                            if refund_id in seen or amount <= 0:
                                valid = False
                                break
                            claims = wallet_store.query(
                                "SELECT agent_id, request_id, owner_id, amount, currency, status "
                                "FROM platform_paypal_refunds WHERE refund_id=?", (refund_id,))
                            if len(claims) != 1:
                                valid = False
                                break
                            claim = claims[0]
                            if (claim["agent_id"] != r["agent_id"]
                                    or claim["request_id"] != r["request_id"]
                                    or claim["owner_id"] != r["ledger_owner"]
                                    or Decimal(claim["amount"]) != amount
                                    or claim["currency"] != r["ledger_currency"]
                                    or claim["status"] not in (
                                        "COMPLETED", "PENDING", "reconciliation_required")):
                                valid = False
                                break
                            # The ledger commit is authoritative across a wallet rollback:
                            # an older pending/unknown projection must remain recoverable.
                            seen.add(refund_id)
                            total += amount
                        amounts_match = valid and request_amount - ledger_amount == total
        except (TypeError, InvalidOperation):
            amounts_match = False
        if not amounts_match or r["ledger_currency"] != r["request_currency"]:
            issues.append(Inconsistency(kind="ledger_terms_differ", agent_id=r["agent_id"],
                                        request_id=r["request_id"]))
    for r in store.query(
            "SELECT p.agent_id, p.request_id FROM provider_operations p LEFT JOIN "
            "provider_orders o ON o.agent_id=p.agent_id AND o.request_id=p.request_id "
            "WHERE p.idempotency_key IS NOT CASE p.operation "
            "WHEN 'create_order' THEN o.create_key WHEN 'capture' THEN o.capture_key END"):
        issues.append(Inconsistency(kind="provider_operation_differs", agent_id=r["agent_id"],
                                    request_id=r["request_id"]))
    if wallet_store is None:
        return issues
    wallet_rows = wallet_store.query("SELECT request_id, agent_id, owner_id, terms_json, terms_sha256 "
                                     "FROM wallet_payment_requests")
    for w in wallet_rows:
        agents = wallet_store.query("SELECT owner_id FROM wallet_agents WHERE agent_id=?",
                                    (w["agent_id"],))
        if not agents or agents[0]["owner_id"] != w["owner_id"]:
            issues.append(Inconsistency(kind="registry_owner_differs", agent_id=w["agent_id"],
                                        request_id=w["request_id"]))
        terms = store.query("SELECT terms_sha256 FROM request_terms WHERE agent_id=? AND "
                            "request_id=?", (w["agent_id"], w["request_id"]))
        if not terms:
            issues.append(Inconsistency(kind="wallet_request_without_ledger_request",
                                        agent_id=w["agent_id"], request_id=w["request_id"]))
        elif terms[0]["terms_sha256"] != w["terms_sha256"]:
            issues.append(Inconsistency(kind="terms_digest_differs",
                                        agent_id=w["agent_id"], request_id=w["request_id"]))
        entry = store.query("SELECT owner_id FROM ledger_entries WHERE agent_id=? AND "
                            "request_id=?", (w["agent_id"], w["request_id"]))
        if entry and entry[0]["owner_id"] != w["owner_id"]:
            issues.append(Inconsistency(kind="owner_differs", agent_id=w["agent_id"],
                                        request_id=w["request_id"]))
        try:
            canonical = ApprovalTerms.model_validate_json(w["terms_json"])
        except ValueError:
            issues.append(Inconsistency(kind="wallet_terms_invalid", agent_id=w["agent_id"],
                                        request_id=w["request_id"]))
        else:
            if canonical.digest() != w["terms_sha256"]:
                issues.append(Inconsistency(kind="wallet_terms_digest_differs",
                                            agent_id=w["agent_id"], request_id=w["request_id"]))
    known = {(w["agent_id"], w["request_id"]) for w in wallet_rows}
    for s in wallet_store.query("SELECT request_id, agent_id, simulation_reference, terms_sha256 "
                                 "FROM wallet_sandbox_simulations"):
        rows = store.query("SELECT state FROM ledger_entries WHERE agent_id=? AND "
                           "request_id=?", (s["agent_id"], s["request_id"]))
        if not rows or rows[0]["state"] in ("reserved", "approved", "released"):
            issues.append(Inconsistency(kind="simulation_without_attempt",
                                        agent_id=s["agent_id"], request_id=s["request_id"]))
        orders = store.query("SELECT provider_order_id FROM provider_orders WHERE "
                             "agent_id=? AND request_id=?", (s["agent_id"], s["request_id"]))
        if not orders or orders[0]["provider_order_id"] != s["simulation_reference"]:
            issues.append(Inconsistency(kind="simulation_reference_differs",
                                        agent_id=s["agent_id"], request_id=s["request_id"]))
        terms = store.query("SELECT terms_sha256 FROM request_terms WHERE agent_id=? AND "
                            "request_id=?", (s["agent_id"], s["request_id"]))
        if not terms or terms[0]["terms_sha256"] != s["terms_sha256"]:
            issues.append(Inconsistency(kind="simulation_terms_differs", agent_id=s["agent_id"],
                                        request_id=s["request_id"]))
    # wallet-prefixed requests that reached the ledger but not the wallet (crash between
    # the two commits): their hold expires on its own; the wallet cannot approve them
    for r in store.query("SELECT agent_id, request_id, state FROM ledger_entries WHERE "
                         "request_id LIKE 'pay\\_%' ESCAPE '\\'"):
        if (r["agent_id"], r["request_id"]) not in known:
            issues.append(Inconsistency(kind="ledger_hold_without_wallet_request",
                                        agent_id=r["agent_id"], request_id=r["request_id"],
                                        detail=r["state"]))
    return issues


def startup_recovery(store: AssessmentStore, wallet_store=None, *,
                     now: datetime | None = None, recover_inflight: bool = True,
                     stale_claim_after: timedelta = timedelta(minutes=5)) -> ConsistencyReport:
    """Recover once at process start; refuse startup on any consistency mismatch.

    Every pending attempt is ambiguous after a restart, even before its deadline: the
    previous process may have died after sending. Quarantine it without releasing funds
    or retrying. A wallet constructed in the same live process can pass
    `recover_inflight=False` to check both databases without declaring a second restart.
    """
    lifecycle = PaymentLifecycleService(store)
    now = now or store.clock()
    report = ConsistencyReport()
    report.stale_claims_recovered = store.recover_stale_claims(now, stale_claim_after)
    if recover_inflight:
        for r in store.query("SELECT agent_id, request_id FROM ledger_entries WHERE "
                             "state='pending' ORDER BY agent_id, request_id"):
            store.mark_reconciliation_required(r["request_id"], "startup_unknown_outcome",
                                               agent_id=r["agent_id"], now=now)
            report.moved_to_reconciliation.append((r["agent_id"], r["request_id"]))
    report.moved_to_reconciliation.extend(lifecycle.expire_and_reconcile(now))
    report.reconciliation_required = [
        (r["agent_id"], r["request_id"]) for r in store.query(
            "SELECT agent_id, request_id FROM ledger_entries WHERE "
            "state='reconciliation_required' ORDER BY updated_at")]
    report.issues = check_consistency(store, wallet_store)
    if report.issues:
        raise ConsistencyError(report)
    return report
