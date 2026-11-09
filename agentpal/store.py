"""Development persistence (SQLite): requests, assessments, the ledger and the audit log.

Replay protection (P0.1) is enforced by the database, not by an in-memory check:
`requests (agent_id, request_id)` is the PRIMARY KEY and a request is *claimed* by
inserting its row inside a `BEGIN IMMEDIATE` transaction before any assessment work starts.
Exactly one submission can claim an id for an agent, across threads, processes (API
workers) and restarts; every other submission by that agent observes the conflict and is
rejected. Request ids are scoped per agent (schema v4, SEC-13): one agent can no longer
squat another agent's id.

Request-scoped methods take `agent_id` as an optional keyword. Without it the id is
resolved, and an id used by more than one agent is refused (`AmbiguousRequestId`): the
store never guesses whose request is meant.

Owner approvals made through an authenticated owner are structured records (`approvals`:
owner_id, session_id, auth method, terms digest, expiry) and are single-use: execution
consumes one in the same transaction that moves the ledger to `pending` (SEC-15). Provider
orders, idempotency keys and provider events live in the same database as the ledger, so
every financial state change is one SQLite transaction (SEC-16/SEC-22).

Free-text fields are not copied into the audit table, and secret-looking strings
are redacted before anything is written.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .features import HistoricalPayment
from .infra_contracts import ProviderEvent, ProviderStatus
from .private_files import ensure_private_db
from .schemas import PaymentRequest, RiskAssessment
from .spending import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    AmbiguousRequestId,
    ApprovalNotUsable,
    ApprovalTerms,
    ApprovalTermsMismatch,
    InvalidLedgerTransition,
    LedgerState,
    LegacyPathDisabled,
    OutcomeConflict,
    OutcomeMismatch,
    RequestNotApprovable,
    ReservationRequest,
    SpendSnapshot,
    budget_period,
)

SCHEMA_VERSION = 4
MAX_UNKNOWN_PROVIDER_EVENTS = 1000

_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)[-_](live|test)?[-_]?[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\bap[kot]_[A-Za-z0-9_\-]{16,}"),  # AgentPal agent/owner/legacy tokens
    re.compile(r"\b(EA|EAA)[A-Za-z0-9]{30,}\b"),  # Meta/WhatsApp-style access tokens
    re.compile(r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+",
               re.IGNORECASE),
    re.compile(r"\b\d{13,19}\b"),  # card-number-like digit runs
]


def redact(text: str) -> str:
    for rx in _SECRET_PATTERNS:
        text = rx.sub("[REDACTED]", text)
    return text


def _redact_obj(obj: object) -> object:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(at: datetime) -> datetime:
    """Every timestamp the ledger stores or compares is UTC. Timestamps are compared as ISO
    strings inside SQLite, so a value with another offset would sort wrongly (SEC-07);
    naive values are ambiguous and refused."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return at.astimezone(timezone.utc)


_SETTLED_STATES = (LedgerState.completed, LedgerState.failed, LedgerState.cancelled)

IDEMPOTENCY_OPERATIONS = ("create_order", "capture")


def idempotency_key(agent_id: str, request_id: str, operation: str) -> str:
    """SEC-20: one provider idempotency key per (agent, request, operation). Never the bare
    request id, never shared between create and capture."""
    if operation not in IDEMPOTENCY_OPERATIONS:
        raise ValueError(f"unknown provider operation {operation!r}")
    digest = hashlib.sha256(
        f"agentpal-v1\x00{agent_id}\x00{request_id}\x00{operation}".encode()).hexdigest()
    return f"ap-{operation.replace('_', '-')}-{digest}"


class IncompatibleDatabase(RuntimeError):
    """The SQLite file was created by an older, incompatible schema."""


class UnknownProviderOrder(LookupError):
    """A provider event names an order AgentPal never created. Recorded and audited."""


_EXPECTED_COLUMNS: dict[str, set[str]] | None = None


def _columns_of(schema: list[str]) -> dict[str, set[str]]:
    con = sqlite3.connect(":memory:")
    try:
        for stmt in schema:
            con.execute(stmt)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        return {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")} for t in tables}
    finally:
        con.close()


def _expected_columns() -> dict[str, set[str]]:
    """Column names of every table in the current schema (built once, in memory)."""
    global _EXPECTED_COLUMNS
    if _EXPECTED_COLUMNS is None:
        _EXPECTED_COLUMNS = _columns_of(_SCHEMA)
    return _EXPECTED_COLUMNS


class AssessmentStore:
    def __init__(self, path: str | Path = ":memory:", busy_timeout_s: float = 30.0,
                 approval_validity: timedelta = timedelta(minutes=15), *,
                 strict_execution: bool = False,
                 attempt_reconcile_after: timedelta = timedelta(minutes=1440)):
        self.path = str(path)
        # set from the trusted spend policy by RiskService (owner decision 6)
        self.approval_validity = approval_validity
        # SEC-16: an attempt still pending after this long goes to reconciliation_required
        self.attempt_reconcile_after = attempt_reconcile_after
        # SEC-15: when True, the legacy unauthenticated attempt path is refused; only
        # begin_execution (which consumes a structured owner approval) may execute.
        self.strict_execution = strict_execution
        # the same trusted clock as the service (set by RiskService); used when a ledger
        # operation is not given an explicit time
        self.clock = _utcnow
        self._lock = threading.RLock()
        # autocommit mode; transactions are opened explicitly with BEGIN IMMEDIATE so
        # that the write lock is taken *before* any read used for a decision.
        ensure_private_db(str(self.path))
        self._conn = sqlite3.connect(self.path, timeout=busy_timeout_s,
                                     isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
        if self.path != ":memory:":
            self._enable_wal(busy_timeout_s)
        self._migrate()

    def _enable_wal(self, timeout_s: float) -> None:
        """Switch a file database to WAL. SQLite can report SQLITE_BUSY for this pragma
        without calling the busy handler while another worker initializes the same new
        file, so retry until the busy timeout instead of failing worker startup."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    # ------------------------------------------------------------- migration
    def _apply(self, schema: list[str], version_label: int) -> None:
        for stmt in schema:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:  # e.g. an index on a missing column
                raise IncompatibleDatabase(
                    f"{self.path}: cannot apply schema v{version_label} ({exc}); table "
                    f"{stmt.split('ON', 1)[-1].split('(', 1)[0].strip()} is "
                    "incompatible. Move the development database aside.") from exc

    def _check_columns(self, expected_by_table: dict[str, set[str]], version: int) -> None:
        # SEC-11: CREATE TABLE IF NOT EXISTS never adds columns. Verify before stamping; a
        # refusal rolls the whole migration back (SQLite DDL is transactional).
        for table, expected in expected_by_table.items():
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(expected - have)
            if missing:
                raise IncompatibleDatabase(
                    f"{self.path}: table {table} lacks columns {', '.join(missing)} "
                    f"(schema v{version} predates them). It is a development database: "
                    "move it aside and restart to create a new one.")

    def _migrate(self) -> None:
        with self._tx():
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            legacy = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_history'"
            ).fetchone()
            if version == 0 and legacy:
                raise IncompatibleDatabase(
                    f"{self.path} uses the pre-P0 schema (request_history). It is a "
                    "development database: move it aside and restart to create a new one.")
            if version > SCHEMA_VERSION:
                raise IncompatibleDatabase(f"{self.path} has newer schema v{version}")
            has_requests = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'"
            ).fetchone()
            if 0 < version < 4 or (version == 0 and has_requests):
                # bring a v2/v3 file to the complete v3 layout, verify it, then rebuild
                # the request-keyed tables with per-agent keys (SEC-13)
                self._apply(_SCHEMA_V3, 3)
                self._check_columns(_columns_of(_SCHEMA_V3), version)
                self._migrate_v3_to_v4()
            # Replace the WIP v4 trigger atomically as well as on fresh databases.
            self._conn.execute("DROP TRIGGER IF EXISTS trg_approvals_single_use")
            self._apply(_SCHEMA, SCHEMA_VERSION)
            self._check_columns(_expected_columns(), version)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate_v3_to_v4(self) -> None:
        """SEC-13: re-key every request-scoped table by (agent_id, request_id). In v3 a
        request_id was globally unique, so the agent of every child row is found by a join
        with `requests`. A child row without a parent request refuses the migration (the
        surrounding transaction rolls everything back and the file stays v3)."""
        c = self._conn
        rekeyed = ["requests", "request_terms", "ledger_entries", "owner_decisions",
                   "payment_attempts", "payment_outcomes", "ledger_events"]
        for t in rekeyed:
            c.execute(f"ALTER TABLE {t} RENAME TO {t}_v3")
        for stmt in _SCHEMA:
            if stmt.lstrip().startswith("CREATE TABLE"):
                name = stmt.split("EXISTS", 1)[1].split("(", 1)[0].strip()
                if name in rekeyed:
                    c.execute(stmt)
        c.execute("INSERT INTO requests SELECT request_id, agent_id, merchant_name, "
                  "merchant_domain, merchant_domain_input, amount, currency, recurring, "
                  "received_at, client_created_at, client_expires_at, effective_expires_at, "
                  "status, recommendation, assessment_json, completed_at FROM requests_v3")
        copies = {
            "request_terms": ("agent_id, request_id, terms_sha256",
                              "r.agent_id, t.request_id, t.terms_sha256"),
            "ledger_entries": ("agent_id, request_id, owner_id, merchant_key, currency, "
                               "amount, state, period, created_at, updated_at, expires_at",
                               "t.agent_id, t.request_id, t.owner_id, t.merchant_key, "
                               "t.currency, t.amount, t.state, t.period, t.created_at, "
                               "t.updated_at, t.expires_at"),
            "owner_decisions": ("id, agent_id, request_id, decision, decided_by, at",
                                "t.id, r.agent_id, t.request_id, t.decision, t.decided_by, "
                                "t.at"),
            "payment_attempts": ("id, agent_id, request_id, provider, provider_reference, at",
                                 "t.id, r.agent_id, t.request_id, t.provider, "
                                 "t.provider_reference, t.at"),
            "payment_outcomes": ("id, agent_id, request_id, outcome, verified, "
                                 "verification_method, provider_reference, at",
                                 "t.id, r.agent_id, t.request_id, t.outcome, t.verified, "
                                 "t.verification_method, t.provider_reference, t.at"),
            "ledger_events": ("id, agent_id, request_id, from_state, to_state, actor, "
                              "reason, at",
                              "t.id, r.agent_id, t.request_id, t.from_state, t.to_state, "
                              "t.actor, t.reason, t.at"),
        }
        for table, (cols, select) in copies.items():
            total = c.execute(f"SELECT COUNT(*) FROM {table}_v3").fetchone()[0]
            moved = c.execute(f"INSERT INTO {table} ({cols}) SELECT {select} FROM "
                              f"{table}_v3 t JOIN requests_v3 r ON r.request_id = "
                              f"t.request_id").rowcount
            if moved != total:
                raise IncompatibleDatabase(
                    f"{self.path}: table {table} has {total - moved} row(s) without a "
                    "parent request; refusing to migrate to v4 (no agent can be assigned).")
        for t in rekeyed:
            c.execute(f"DROP TABLE {t}_v3")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (in-process lock + SQLite RESERVED lock)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---------------------------------------------------------- id scoping
    def _agent_of(self, c: sqlite3.Connection, request_id: str,
                  agent_id: str | None) -> str | None:
        """SEC-13: resolve the agent that owns `request_id`. An explicit agent is used as
        given; without one, an id shared by several agents is refused."""
        if agent_id is not None:
            return agent_id
        rows = c.execute("SELECT agent_id FROM requests WHERE request_id=? LIMIT 2",
                         (request_id,)).fetchall()
        if len(rows) > 1:
            raise AmbiguousRequestId(
                f"{request_id} is used by more than one agent; name the agent")
        return rows[0][0] if rows else None

    def resolve_agent(self, request_id: str, agent_id: str | None = None) -> str | None:
        with self._lock:
            return self._agent_of(self._conn, request_id, agent_id)

    # ---------------------------------------------------------------- claims
    def claim_request(self, req: PaymentRequest, received_at: datetime,
                      reservation: "ReservationRequest | None" = None
                      ) -> tuple[bool, SpendSnapshot | None]:
        """Atomically claim (`req.agent_id`, `req.request_id`) and, in the SAME transaction,
        read the trusted spend snapshot and (if the financial checks pass) reserve the
        amount.

        Returns (claimed, snapshot). claimed is True only for the first submission of an
        id by this agent. Holding the write lock across read-check-insert makes concurrent
        requests unable to overspend the remaining budget (P0.1 + P0.2)."""
        received_at = _as_utc(received_at)
        try:
            with self._tx() as c:
                self._insert_request(c, req, received_at)
                snapshot = None
                if reservation is not None:
                    self._expire_reservations(c, received_at)
                    snapshot = self._snapshot(c, req, received_at, reservation)
                    if snapshot.policy_found and reservation.within_limits(req, snapshot):
                        self._reserve(c, req, received_at, reservation)
                        snapshot = snapshot.model_copy(update={"reservation_created": True})
            return True, snapshot
        except sqlite3.IntegrityError:
            return False, None

    def _insert_request(self, c: sqlite3.Connection, req: PaymentRequest,
                        received_at: datetime) -> None:
        c.execute(
            "INSERT INTO requests (request_id, agent_id, merchant_name, "
            "merchant_domain, merchant_domain_input, amount, currency, recurring, received_at, "
            "client_created_at, client_expires_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'claimed')",
            (req.request_id, req.agent_id, req.merchant_name, req.merchant_domain,
             redact(req.merchant_domain_input) if req.merchant_domain_input else None,
             str(req.amount), req.currency, int(req.recurring),
             received_at.isoformat(),
             req.created_at.isoformat() if req.created_at else None,
             req.expires_at.isoformat() if req.expires_at else None))
        c.execute("INSERT INTO request_terms (agent_id, request_id, terms_sha256) "
                  "VALUES (?,?,?)",
                  (req.agent_id, req.request_id, ApprovalTerms.from_request(req).digest()))

    @staticmethod
    def _check_terms(c: sqlite3.Connection, agent_id: str, request_id: str,
                     terms: ApprovalTerms) -> str:
        """Final owner decision 5: the presented terms must be exactly the assessed ones.
        Returns the bound digest."""
        if not isinstance(terms, ApprovalTerms):
            raise ApprovalTermsMismatch(f"no terms presented for {request_id}")
        row = c.execute("SELECT terms_sha256 FROM request_terms WHERE agent_id=? AND "
                        "request_id=?", (agent_id, request_id)).fetchone()
        if row is None or terms.request_id != request_id or terms.agent_id != agent_id \
                or not hmac.compare_digest(row[0], terms.digest()):
            raise ApprovalTermsMismatch(
                f"terms presented for {request_id} do not match the assessed request")
        return row[0]

    # ---------------------------------------------------------------- ledger
    def _expire_reservations(self, c: sqlite3.Connection, now: datetime) -> None:
        """Release holds whose validity has lapsed: unapproved reservations at the request's
        effective expiry, approvals at decision time + approval validity (decision 6).
        SEC-16: a payment attempt (`pending`) past its reconcile deadline is NOT released
        (its provider outcome is unknown); it moves to `reconciliation_required`, which this
        method never touches again."""
        rows = c.execute("SELECT agent_id, request_id, state FROM ledger_entries WHERE "
                         "state IN ('reserved','approved','pending') AND expires_at <= ?",
                         (now.isoformat(),)).fetchall()
        for r in rows:
            if r["state"] == LedgerState.pending.value:
                self._set_state(c, r["agent_id"], r["request_id"],
                                LedgerState.reconciliation_required, "system",
                                "attempt_outcome_unknown_deadline", now)
                continue
            reason = "approval_expired" if r["state"] == "approved" else "reservation_expired"
            self._set_state(c, r["agent_id"], r["request_id"], LedgerState.released,
                            "system", reason, now)

    def _snapshot(self, c: sqlite3.Connection, req: PaymentRequest, now: datetime,
                  rr: ReservationRequest) -> SpendSnapshot:
        if rr.limits is None:
            return SpendSnapshot.missing(req.currency)
        active = tuple(s.value for s in ACTIVE_STATES)
        rows = c.execute(
            f"SELECT amount, state, period, created_at, merchant_key FROM ledger_entries "
            f"WHERE agent_id=? AND currency=? AND state IN ({','.join('?' * len(active))})",
            (req.agent_id, req.currency, *active)).fetchall()
        period = budget_period(now)
        window_start = now - timedelta(hours=rr.rolling_window_hours)
        committed = completed = window = merchant = Decimal(0)
        for r in rows:  # exact Decimal arithmetic in Python (SQLite SUM would use floats)
            amt = Decimal(r["amount"])
            if r["period"] == period:
                committed += amt
                if r["state"] == LedgerState.completed.value:
                    completed += amt
            if datetime.fromisoformat(r["created_at"]) >= window_start:
                window += amt
                if r["merchant_key"] == rr.merchant_key:
                    merchant += amt
        return SpendSnapshot(
            currency=req.currency, policy_found=True, owner_id=rr.owner_id,
            monthly_budget=rr.limits.monthly_budget,
            rolling_window_hours=rr.rolling_window_hours,
            rolling_window_limit=rr.limits.rolling_window_limit,
            per_transaction_hard_limit=rr.limits.per_transaction_hard_limit,
            per_transaction_review_limit=rr.limits.per_transaction_review_limit,
            committed_in_period=committed, completed_in_period=completed,
            rolling_window_committed=window, merchant_window_committed=merchant,
            demo_default_budget=rr.demo_default_budget)

    def _reserve(self, c: sqlite3.Connection, req: PaymentRequest, now: datetime,
                 rr: ReservationRequest) -> None:
        c.execute(
            "INSERT INTO ledger_entries (agent_id, request_id, owner_id, merchant_key, "
            "currency, amount, state, period, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.agent_id, req.request_id, rr.owner_id, rr.merchant_key, req.currency,
             str(req.amount), LedgerState.reserved.value, budget_period(now),
             now.isoformat(), now.isoformat(), rr.expires_at.isoformat()))
        self._ledger_event(c, req.agent_id, req.request_id, None, LedgerState.reserved,
                           "system", "request_within_limits", now)

    def _set_state(self, c: sqlite3.Connection, agent_id: str, request_id: str,
                   to: LedgerState, actor: str, reason: str | None, now: datetime) -> None:
        row = c.execute("SELECT state FROM ledger_entries WHERE agent_id=? AND request_id=?",
                        (agent_id, request_id)).fetchone()
        if row is None:
            raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
        current = LedgerState(row[0])
        if to not in ALLOWED_TRANSITIONS[current]:
            raise InvalidLedgerTransition(f"{current.value} -> {to.value} is not allowed")
        c.execute("UPDATE ledger_entries SET state=?, updated_at=? WHERE agent_id=? AND "
                  "request_id=?", (to.value, now.isoformat(), agent_id, request_id))
        self._ledger_event(c, agent_id, request_id, current, to, actor, reason, now)

    def _ledger_event(self, c: sqlite3.Connection, agent_id: str, request_id: str,
                      frm: LedgerState | None, to: LedgerState, actor: str,
                      reason: str | None, now: datetime) -> None:
        c.execute("INSERT INTO ledger_events (agent_id, request_id, from_state, to_state, "
                  "actor, reason, at) VALUES (?,?,?,?,?,?,?)",
                  (agent_id, request_id, frm.value if frm else None, to.value, actor, reason,
                   now.isoformat()))

    def _require_agent(self, c: sqlite3.Connection, request_id: str,
                       agent_id: str | None) -> str:
        agent = self._agent_of(c, request_id, agent_id)
        if agent is None:
            raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
        return agent

    def transition_ledger(self, request_id: str, to: LedgerState, actor: str,
                          reason: str | None = None, now: datetime | None = None, *,
                          agent_id: str | None = None) -> None:
        """Release (cancel) a hold, with an append-only audit event. This is the ONLY
        generic transition (SEC-14): approval, payment attempt and completion must go
        through the bound methods, which check terms and require verified provider
        evidence. Release is impossible from `pending` or `reconciliation_required`."""
        if to != LedgerState.released:
            raise InvalidLedgerTransition(
                f"transition_ledger may only release a hold, not move it to {to.value}; use "
                "record_owner_decision, record_payment_attempt or record_payment_outcome")
        now = _as_utc(now or self.clock())
        self.expire_holds(now)
        with self._tx() as c:
            agent = self._require_agent(c, request_id, agent_id)
            self._set_state(c, agent, request_id, to, actor, reason, now)

    def ledger_state(self, request_id: str, *, agent_id: str | None = None
                     ) -> LedgerState | None:
        entry = self.ledger_entry(request_id, agent_id=agent_id)
        return LedgerState(entry["state"]) if entry else None

    def ledger_entry(self, request_id: str, *, agent_id: str | None = None) -> dict | None:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            row = self._conn.execute("SELECT * FROM ledger_entries WHERE agent_id=? AND "
                                     "request_id=?", (agent, request_id)).fetchone()
        return dict(row) if row else None

    def ledger_events(self, request_id: str, *, agent_id: str | None = None) -> list[dict]:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            rows = self._conn.execute(
                "SELECT * FROM ledger_events WHERE agent_id=? AND request_id=? ORDER BY id",
                (agent, request_id)).fetchall()
        return [dict(r) for r in rows]

    def spend_totals(self, agent_id: str, currency: str, at: datetime) -> dict[str, Decimal]:
        """Committed exposure and completed spending for the budget period containing `at`."""
        at = _as_utc(at)
        with self._tx() as c:
            self._expire_reservations(c, at)
            rows = c.execute("SELECT amount, state FROM ledger_entries WHERE agent_id=? "
                             "AND currency=? AND period=?",
                             (agent_id, currency, budget_period(at))).fetchall()
        active = {s.value for s in ACTIVE_STATES}
        return {
            "committed": sum((Decimal(r[0]) for r in rows if r[1] in active), Decimal(0)),
            "completed": sum((Decimal(r[0]) for r in rows
                              if r[1] == LedgerState.completed.value), Decimal(0)),
        }

    # ---------------------------------------------------- trust events (P0.4)
    # Observed requests, our own assessments, owner decisions, payment attempts and
    # payment outcomes are separate, append-only records. Only a VERIFIED completed
    # outcome makes a payment part of trusted history.
    def record_owner_decision(self, request_id: str, decision: str, decided_by: str,
                              terms: ApprovalTerms | None = None,
                              now: datetime | None = None, *,
                              agent_id: str | None = None) -> None:
        """LEGACY (Phase-1 harness) decision with a free-text `decided_by`. An approval is
        bound to the exact terms the owner saw (final decision 5); a decline needs none.
        It writes NO structured approval record, so it can never be executed through
        `begin_execution` (SEC-15). Use `approve_authenticated` / `decline_authenticated`."""
        if decision not in ("approved", "declined"):
            raise ValueError("decision must be 'approved' or 'declined'")
        if decision == "approved" and terms is None:
            raise ValueError("an approval must be bound to the request terms")
        now = _as_utc(now or self.clock())
        self.expire_holds(now)  # committed first: an expired hold cannot be approved
        with self._tx() as c:
            agent = self._require_agent(c, request_id, agent_id)
            if terms is not None:
                self._check_terms(c, agent, request_id, terms)
            self._decide(c, agent, request_id, decision, f"owner:{decided_by}", now)
            c.execute("INSERT INTO owner_decisions (agent_id, request_id, decision, "
                      "decided_by, at) VALUES (?,?,?,?,?)",
                      (agent, request_id, decision, decided_by, now.isoformat()))

    def _decide(self, c: sqlite3.Connection, agent: str, request_id: str, decision: str,
                actor: str, now: datetime) -> datetime | None:
        """Move the ledger for an owner decision; returns the approval's expiry."""
        to = LedgerState.approved if decision == "approved" else LedgerState.released
        request_expiry = None
        if to == LedgerState.approved:
            request_expiry = self._approvable_until(c, agent, request_id)
        self._set_state(c, agent, request_id, to, actor, f"owner_{decision}", now)
        if to != LedgerState.approved:
            return None
        # SEC-06: the approval is bound to the request's expiry too; it never
        # outlives the server-computed effective expiry of what was assessed
        expires = min(now + self.approval_validity, request_expiry)
        c.execute("UPDATE ledger_entries SET expires_at=? WHERE agent_id=? AND request_id=?",
                  (expires.isoformat(), agent, request_id))
        return expires

    def approve_authenticated(self, request_id: str, terms: ApprovalTerms, *, agent_id: str,
                              owner_id: str, session_id: str, auth_method: str,
                              org_id: str | None = None, mfa_satisfied: bool = False,
                              now: datetime | None = None) -> dict:
        """SEC-15: an approval by an AUTHENTICATED owner. In one transaction: the terms are
        checked, the ledger owner must be this owner, the owner can never be the requesting
        agent, the ledger moves to `approved`, and a structured, single-use, time-limited
        `approvals` record is written. Returns that record."""
        now = _as_utc(now or self.clock())
        self.expire_holds(now)
        with self._tx() as c:
            digest = self._check_terms(c, agent_id, request_id, terms)
            entry = c.execute("SELECT owner_id FROM ledger_entries WHERE agent_id=? AND "
                              "request_id=?", (agent_id, request_id)).fetchone()
            if entry is None:
                raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
            if owner_id == agent_id:
                raise RequestNotApprovable("an agent can never approve its own request")
            if entry["owner_id"] is None or not hmac.compare_digest(entry["owner_id"],
                                                                   owner_id):
                raise RequestNotApprovable("the ledger entry belongs to another owner")
            expires = self._decide(c, agent_id, request_id, "approved",
                                   f"owner:{owner_id}|token:{session_id}", now)
            c.execute("INSERT INTO owner_decisions (agent_id, request_id, decision, "
                      "decided_by, owner_id, session_id, auth_method, at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (agent_id, request_id, "approved", f"{owner_id}|token:{session_id}",
                       owner_id, session_id, auth_method, now.isoformat()))
            cur = c.execute(
                "INSERT INTO approvals (agent_id, request_id, owner_id, org_id, session_id, "
                "auth_method, mfa_satisfied, terms_sha256, approved_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (agent_id, request_id, owner_id, org_id, session_id, auth_method,
                 int(mfa_satisfied), digest, now.isoformat(), expires.isoformat()))
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?",
                            (cur.lastrowid,)).fetchone()
        return dict(row)

    def decline_authenticated(self, request_id: str, *, agent_id: str, owner_id: str,
                              session_id: str, auth_method: str,
                              now: datetime | None = None) -> None:
        now = _as_utc(now or self.clock())
        self.expire_holds(now)
        with self._tx() as c:
            entry = c.execute("SELECT owner_id FROM ledger_entries WHERE agent_id=? AND "
                              "request_id=?", (agent_id, request_id)).fetchone()
            if entry is None:
                raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
            if entry["owner_id"] is None or not hmac.compare_digest(entry["owner_id"],
                                                                   owner_id):
                raise RequestNotApprovable("the ledger entry belongs to another owner")
            self._decide(c, agent_id, request_id, "declined",
                         f"owner:{owner_id}|token:{session_id}", now)
            c.execute("INSERT INTO owner_decisions (agent_id, request_id, decision, "
                      "decided_by, owner_id, session_id, auth_method, at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (agent_id, request_id, "declined", f"{owner_id}|token:{session_id}",
                       owner_id, session_id, auth_method, now.isoformat()))

    def approvals(self, request_id: str, *, agent_id: str | None = None) -> list[dict]:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            rows = self._conn.execute("SELECT * FROM approvals WHERE agent_id=? AND "
                                      "request_id=? ORDER BY approval_id",
                                      (agent, request_id)).fetchall()
        return [dict(r) for r in rows]

    def owner_decision_records(self, request_id: str, *, agent_id: str | None = None
                               ) -> list[dict]:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            rows = self._conn.execute("SELECT * FROM owner_decisions WHERE agent_id=? AND "
                                      "request_id=? ORDER BY id",
                                      (agent, request_id)).fetchall()
        return [dict(r) for r in rows]

    def _approvable_until(self, c: sqlite3.Connection, agent_id: str,
                          request_id: str) -> datetime:
        """SEC-05: only a request whose assessment finished without `block` can be approved.
        The reservation exists from the claim onwards, i.e. while the rules are still
        running; an approval in that window would survive a later `block`."""
        row = c.execute("SELECT status, recommendation, effective_expires_at FROM requests "
                        "WHERE agent_id=? AND request_id=?",
                        (agent_id, request_id)).fetchone()
        if row is None or row["status"] != "assessed" or row["recommendation"] == "block" \
                or not row["effective_expires_at"]:
            raise RequestNotApprovable(
                f"{request_id} has no finished, non-blocking assessment to approve")
        return _as_utc(datetime.fromisoformat(row["effective_expires_at"]))

    def record_payment_attempt(self, request_id: str, provider: str,
                               provider_reference: str, *, terms: ApprovalTerms,
                               now: datetime | None = None,
                               agent_id: str | None = None) -> None:
        """LEGACY (Phase-1 harness): an attempt must present the approved terms; it is never
        completed spending. It does not require a structured owner approval, so a strict
        store refuses it (SEC-15); use `begin_execution`."""
        if self.strict_execution:
            raise LegacyPathDisabled("record_payment_attempt is disabled on a strict store; "
                                     "use begin_execution")
        now = _as_utc(now or self.clock())
        self.expire_holds(now)  # committed first: an expired approval cannot be executed
        with self._tx() as c:
            agent = self._require_agent(c, request_id, agent_id)
            self._check_terms(c, agent, request_id, terms)
            self._to_pending(c, agent, request_id, f"provider:{provider}", now)
            c.execute("INSERT INTO payment_attempts (agent_id, request_id, provider, "
                      "provider_reference, at) VALUES (?,?,?,?,?)",
                      (agent, request_id, provider, provider_reference, now.isoformat()))

    def _to_pending(self, c: sqlite3.Connection, agent: str, request_id: str, actor: str,
                    now: datetime) -> None:
        self._set_state(c, agent, request_id, LedgerState.pending, actor,
                        "payment_attempt_submitted", now)
        # SEC-16: from now on `expires_at` is the reconcile deadline of the attempt
        c.execute("UPDATE ledger_entries SET expires_at=? WHERE agent_id=? AND request_id=?",
                  ((now + self.attempt_reconcile_after).isoformat(), agent, request_id))

    # ---------------------------------------------------- execution (SEC-15/16/20)
    def begin_execution(self, request_id: str, terms: ApprovalTerms, provider: str, *,
                        agent_id: str, now: datetime | None = None) -> dict:
        """The ONLY gated path into `pending`. In one transaction:
        * the terms must be exactly the assessed ones and the ledger `approved`;
        * an authenticated owner approval for exactly these terms must exist, be unexpired
          and unconsumed, and belong to the ledger's owner; it is consumed here (single use);
        * the ledger moves to `pending` with a reconcile deadline;
        * the provider order row is created with BOTH idempotency keys (create_order and
          capture), persisted before any provider call (SEC-20).
        A crash before the commit leaves nothing; after it, the entry is `pending` and can
        only end through verified evidence or `reconciliation_required`."""
        now = _as_utc(now or self.clock())
        self.expire_holds(now)
        with self._tx() as c:
            digest = self._check_terms(c, agent_id, request_id, terms)
            entry = c.execute("SELECT state, owner_id FROM ledger_entries WHERE agent_id=? "
                              "AND request_id=?", (agent_id, request_id)).fetchone()
            if entry is None or entry["state"] != LedgerState.approved.value:
                raise InvalidLedgerTransition(
                    f"{request_id} is not in state approved (expired, executed or declined)")
            approval = c.execute(
                "SELECT * FROM approvals WHERE agent_id=? AND request_id=? AND "
                "consumed_at IS NULL AND expires_at > ? AND approved_at <= ? "
                "ORDER BY approval_id DESC LIMIT 1",
                # E2E-T4-5: an approval is usable only inside [approved_at, expires_at). A clock
                # stepped backwards must not consume it before it existed (startup recovery
                # enforces the same window and would otherwise refuse to start).
                (agent_id, request_id, now.isoformat(), now.isoformat())).fetchone()
            if approval is None or not hmac.compare_digest(approval["terms_sha256"], digest) \
                    or approval["owner_id"] != entry["owner_id"] \
                    or approval["owner_id"] == agent_id:
                raise ApprovalNotUsable(
                    f"no unexpired, unconsumed authenticated approval for {request_id}")
            create_key = idempotency_key(agent_id, request_id, "create_order")
            capture_key = idempotency_key(agent_id, request_id, "capture")
            cur = c.execute("UPDATE approvals SET consumed_at=?, consumed_by=? WHERE "
                            "approval_id=? AND consumed_at IS NULL",
                            (now.isoformat(), create_key, approval["approval_id"]))
            if cur.rowcount != 1:
                raise ApprovalNotUsable(f"the approval for {request_id} was already used")
            self._to_pending(c, agent_id, request_id, f"provider:{provider}", now)
            c.execute("INSERT INTO payment_attempts (agent_id, request_id, provider, "
                      "provider_reference, at) VALUES (?,?,?,?,?)",
                      (agent_id, request_id, provider, create_key, now.isoformat()))
            c.execute("INSERT INTO provider_orders (agent_id, request_id, provider, "
                      "create_key, capture_key, last_status, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (agent_id, request_id, provider, create_key, capture_key,
                       "not_submitted", now.isoformat(), now.isoformat()))
        return {"agent_id": agent_id, "request_id": request_id, "provider": provider,
                "approval_id": approval["approval_id"], "terms_sha256": digest,
                "create_key": create_key, "capture_key": capture_key}

    def provider_order(self, request_id: str, *, agent_id: str | None = None) -> dict | None:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            row = self._conn.execute("SELECT * FROM provider_orders WHERE agent_id=? AND "
                                     "request_id=?", (agent, request_id)).fetchone()
        return dict(row) if row else None

    def claim_provider_operation(self, request_id: str, *, agent_id: str, provider: str,
                                 operation: str, ticket: dict | None = None,
                                 payer_approval: ProviderEvent | None = None,
                                 now: datetime | None = None) -> dict:
        """Commit an invocation marker before I/O. A claimed operation is never retried.

        The marker and state check share the SQLite write transaction, including across
        workers. Tickets are untrusted values: compare every field with the ledger.
        A crash after this commit is deliberately ambiguous and needs reconciliation.
        """
        if operation not in ("create_order", "capture"):
            raise InvalidLedgerTransition("unknown provider operation")
        now = _as_utc(now or self.clock())
        self.expire_holds(now)
        with self._tx() as c:
            row = c.execute("SELECT o.*, l.state, l.amount, l.currency, l.expires_at, "
                            "l.owner_id, a.consumed_at, a.consumed_by, "
                            "a.owner_id AS approval_owner, a.approval_id, a.terms_sha256 "
                            "FROM provider_orders o JOIN ledger_entries l ON "
                            "l.agent_id=o.agent_id AND l.request_id=o.request_id "
                            "JOIN approvals a ON a.agent_id=o.agent_id AND "
                            "a.request_id=o.request_id WHERE o.agent_id=? AND o.request_id=?",
                            (agent_id, request_id)).fetchone()
            if row is None or row["state"] != "pending" or row["provider"] != provider:
                raise InvalidLedgerTransition("provider operation is not allowed")
            if operation == "create_order":
                if row["provider_order_id"] is not None or ticket is None:
                    raise InvalidLedgerTransition("create operation already submitted")
                if any(ticket.get(k) != row[k] for k in (
                        "agent_id", "request_id", "provider", "approval_id",
                        "terms_sha256", "create_key", "capture_key")):
                    raise InvalidLedgerTransition("execution ticket differs from the ledger")
                try:
                    self._check_terms(c, agent_id, request_id, ticket.get("terms"))
                except ApprovalTermsMismatch:
                    raise InvalidLedgerTransition("execution ticket terms differ") from None
            elif not row["provider_order_id"]:
                raise InvalidLedgerTransition("capture needs a recorded provider order")
            if operation == "capture":
                proof = payer_approval
                if (not isinstance(proof, ProviderEvent) or proof.verified is not True
                        or proof.environment != "sandbox" or proof.provider != provider
                        or proof.provider_order_id != row["provider_order_id"]
                        or proof.event_type != "ORDER_STATUS_READ"
                        or proof.status != ProviderStatus.approved_by_payer
                        or proof.amount is None or proof.amount != Decimal(row["amount"])
                        or proof.currency != row["currency"]
                        or proof.received_at.tzinfo is None
                        or not now - timedelta(seconds=30) <= _as_utc(proof.received_at)
                        <= now + timedelta(seconds=5)
                        or row["expires_at"] <= now.isoformat()
                        or row["consumed_at"] is None
                        or row["consumed_by"] != row["create_key"]
                        or row["approval_owner"] != row["owner_id"]):
                    raise InvalidLedgerTransition("capture requires an exact verified payer read")
                terms = c.execute("SELECT terms_sha256 FROM request_terms WHERE agent_id=? "
                                  "AND request_id=?", (agent_id, request_id)).fetchone()
                if terms is None or not hmac.compare_digest(terms[0], row["terms_sha256"]):
                    raise InvalidLedgerTransition("capture approval terms differ")
                assessed = c.execute("SELECT amount,currency FROM requests WHERE agent_id=? "
                                     "AND request_id=?", (agent_id, request_id)).fetchone()
                if (assessed is None or proof.amount != Decimal(assessed["amount"])
                        or proof.currency != assessed["currency"]):
                    raise InvalidLedgerTransition("capture differs from the assessed payment")
            if c.execute("SELECT 1 FROM provider_operations WHERE agent_id=? AND "
                         "request_id=? AND operation=?",
                         (agent_id, request_id, operation)).fetchone():
                raise InvalidLedgerTransition("provider operation already invoked")
            c.execute("INSERT INTO provider_operations (agent_id, request_id, operation, "
                      "idempotency_key, invoked_at) VALUES (?,?,?,?,?)",
                      (agent_id, request_id, operation,
                       row["create_key" if operation == "create_order" else "capture_key"],
                       now.isoformat()))
            if operation == "capture":
                c.execute("INSERT INTO provider_capture_checks (agent_id, request_id, "
                          "event_id, raw_sha256, amount, currency, observed_at, checked_at) "
                          "VALUES (?,?,?,?,?,?,?,?)", (agent_id, request_id, proof.event_id,
                          proof.raw_sha256, str(proof.amount), proof.currency,
                          _as_utc(proof.received_at).isoformat(), now.isoformat()))
                c.execute("UPDATE provider_orders SET last_status='approved_by_payer', "
                          "updated_at=? WHERE agent_id=? AND request_id=?",
                          (now.isoformat(), agent_id, request_id))
            return dict(row)

    def record_provider_order(self, request_id: str, *, agent_id: str, provider: str,
                              provider_order_id: str, idempotency_key: str, status: str,
                              payer_action_url: str | None = None,
                              now: datetime | None = None) -> None:
        """Store the provider's order id for an attempt. The key must be the persisted
        create_order key and the order id cannot be replaced once set."""
        now = _as_utc(now or self.clock())
        if payer_action_url is not None:
            from urllib.parse import urlsplit
            try:
                parts = urlsplit(payer_action_url)
                host = (parts.hostname or "").lower()
                valid = (len(payer_action_url) <= 2048 and parts.scheme == "https"
                    and (host == "sandbox.paypal.com" or host.endswith(".sandbox.paypal.com"))
                    and parts.port in (None, 443) and parts.username is None
                    and parts.password is None and not any(ord(c) < 33 for c in payer_action_url))
            except ValueError:
                valid = False
            if not valid:
                raise OutcomeMismatch("payer approval URL must be sandbox HTTPS")
        with self._tx() as c:
            row = c.execute("SELECT * FROM provider_orders WHERE agent_id=? AND request_id=?",
                            (agent_id, request_id)).fetchone()
            if row is None or row["provider"] != provider \
                    or not hmac.compare_digest(row["create_key"], idempotency_key):
                raise OutcomeMismatch(f"provider order does not match the attempt of "
                                      f"{request_id}")
            if row["provider_order_id"] not in (None, provider_order_id):
                raise OutcomeConflict(f"{request_id} already has a different provider order")
            c.execute("UPDATE provider_orders SET provider_order_id=?, last_status=?, "
                      "updated_at=? WHERE agent_id=? AND request_id=?",
                      (provider_order_id, status, now.isoformat(), agent_id, request_id))
            c.execute("INSERT INTO provider_order_links (agent_id, request_id, payer_action_url) "
                      "VALUES (?,?,?) ON CONFLICT(agent_id,request_id) DO UPDATE SET "
                      "payer_action_url=COALESCE(excluded.payer_action_url,payer_action_url)",
                      (agent_id, request_id, payer_action_url))

    def provider_payer_url(self, request_id: str, *, agent_id: str) -> str | None:
        rows = self.query("SELECT payer_action_url FROM provider_order_links WHERE agent_id=? "
                          "AND request_id=?", (agent_id, request_id))
        return rows[0]["payer_action_url"] if rows else None

    def mark_reconciliation_required(self, request_id: str, reason: str, *, agent_id: str,
                                     now: datetime | None = None) -> LedgerState:
        """SEC-16: the provider outcome is unknown. Only a `pending` entry moves; an entry
        already in reconciliation_required (or settled) is left as it is."""
        now = _as_utc(now or self.clock())
        with self._tx() as c:
            row = c.execute("SELECT state FROM ledger_entries WHERE agent_id=? AND "
                            "request_id=?", (agent_id, request_id)).fetchone()
            if row is None:
                raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
            if row[0] == LedgerState.pending.value:
                self._set_state(c, agent_id, request_id, LedgerState.reconciliation_required,
                                "system", reason, now)
                return LedgerState.reconciliation_required
            return LedgerState(row[0])

    def cancel_before_provider_order(self, request_id: str, reason: str, *, agent_id: str,
                                     now: datetime | None = None) -> LedgerState:
        """A DEFINITE provider refusal of create_order (outcome_unknown=False) that returned
        no provider order id: nothing exists at the provider that could ever be captured,
        so the attempt is cancelled and the hold returned. Refused (and the entry left
        alone) as soon as any provider order id is known."""
        now = _as_utc(now or self.clock())
        with self._tx() as c:
            row = c.execute("SELECT l.state, o.provider_order_id FROM ledger_entries l JOIN "
                            "provider_orders o ON o.agent_id = l.agent_id AND "
                            "o.request_id = l.request_id WHERE l.agent_id=? AND "
                            "l.request_id=?", (agent_id, request_id)).fetchone()
            if row is None or row["state"] != LedgerState.pending.value \
                    or row["provider_order_id"] is not None:
                raise InvalidLedgerTransition(
                    f"{request_id} cannot be cancelled: a provider order may exist")
            self._set_state(c, agent_id, request_id, LedgerState.cancelled, "system", reason,
                            now)
            c.execute("UPDATE provider_orders SET last_status='rejected_before_order', "
                      "updated_at=? WHERE agent_id=? AND request_id=?",
                      (now.isoformat(), agent_id, request_id))
        return LedgerState.cancelled

    # -------------------------------------------------- provider events (SEC-16)
    def apply_provider_event(self, *, provider: str, event_id: str, event_type: str,
                             provider_order_id: str, provider_capture_id: str | None,
                             status: str, amount: Decimal | None, currency: str | None,
                             verified: bool, raw_sha256: str, received_at: datetime,
                             now: datetime | None = None) -> tuple[str, dict | None]:
        """Apply one provider event idempotently. Returns (disposition, ledger entry).

        * dedup by (provider, event_id): a redelivery changes nothing (`duplicate`);
        * an unknown order is recorded (`unknown_order`) and raises UnknownProviderOrder;
          a redelivery after the order became known is processed normally;
        * an UNVERIFIED event is recorded and never changes the ledger; on a pending entry
          it flags the order for a verified status re-check;
        * amount/currency must equal the approved terms (required for every final event);
        * a verified final status settles pending/reconciliation_required; a stale
          non-final status is ignored (`out_of_order_ignored`); a verified final status that
          contradicts the settled one moves the entry to reconciliation_required."""
        now = _as_utc(now or self.clock())
        received_at = _as_utc(received_at)
        with self._tx() as c:
            prior = c.execute("SELECT e.*, r.provider_capture_id FROM provider_events e "
                              "LEFT JOIN provider_event_captures r USING (provider,event_id) "
                              "WHERE e.provider=? AND e.event_id=?",
                              (provider, event_id)).fetchone()
            upgrade = prior is not None and not prior["verified"] and verified
            if prior is not None and not upgrade:
                collision = (prior["provider_order_id"] != provider_order_id
                             or prior["status"] != status
                             or prior["provider_capture_id"] != provider_capture_id
                             or prior["currency"] != currency
                             or (None if amount is None else Decimal(str(amount))) !=
                             (None if prior["amount"] is None else Decimal(prior["amount"])))
                order = c.execute("SELECT agent_id, request_id FROM provider_orders WHERE "
                                  "provider=? AND provider_order_id=?",
                                  (provider, prior["provider_order_id"])).fetchone()
                if collision or prior["disposition"] != "unknown_order":
                    if collision and prior["verified"] and verified and order is not None:
                        entry = self._entry(c, order)
                        if entry and entry["state"] in (
                                "pending", "completed", "failed", "cancelled", "reversed"):
                            self._set_state(c, order["agent_id"], order["request_id"],
                                            LedgerState.reconciliation_required,
                                            f"verified:provider_event:{provider}",
                                            "conflicting_event_id", now)
                    self._write_outcome_audit(
                        c, order["request_id"] if order else "unknown",
                        order["agent_id"] if order else None,
                        "provider_event_collision_refused" if collision else
                        "provider_event_duplicate_ignored", now)
                    return "collision" if collision else "duplicate", (
                        self._entry(c, order) if order else None)
            if upgrade:
                # An unauthenticated delivery must not reserve an authentic webhook id.
                # Preserve the refusal in the append-only audit; verified evidence wins.
                self._write_outcome_audit(c, prior["request_id"] or "unknown",
                                          prior["agent_id"], "provider_event_verified_upgrade",
                                          now)
            order = c.execute("SELECT * FROM provider_orders WHERE provider=? AND "
                              "provider_order_id=?", (provider, provider_order_id)).fetchone()
            if order is None:
                # Bound signature-valid events for unrelated/unknown orders, including
                # capture metadata. Keep the append-only audit and all known financial
                # evidence. An evicted event can be redelivered once its order is known.
                count = c.execute("SELECT COUNT(*) FROM provider_events WHERE "
                                  "disposition='unknown_order'").fetchone()[0]
                remove = max(0, count - MAX_UNKNOWN_PROVIDER_EVENTS + 1)
                if remove:
                    oldest = ("SELECT provider,event_id FROM provider_events WHERE "
                              "disposition='unknown_order' ORDER BY received_at,rowid LIMIT ?")
                    c.execute("DELETE FROM provider_event_captures WHERE (provider,event_id) "
                              "IN (" + oldest + ")", (remove,))
                    c.execute("DELETE FROM provider_events WHERE (provider,event_id) IN ("
                              + oldest + ")", (remove,))
            values = (provider, event_id, event_type, provider_order_id, status,
                      None if amount is None else str(amount), currency, int(verified),
                      raw_sha256, received_at.isoformat(), now.isoformat())
            c.execute("INSERT OR REPLACE INTO provider_event_captures "
                      "(provider,event_id,provider_capture_id) VALUES (?,?,?)",
                      (provider, event_id, provider_capture_id))
            if order is None:
                c.execute("INSERT OR REPLACE INTO provider_events (provider, event_id, "
                          "event_type, provider_order_id, status, amount, currency, verified, "
                          "raw_sha256, received_at, processed_at, disposition) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'unknown_order')", values)
                self._write_outcome_audit(c, "unknown", None,
                                          "provider_event_rejected:unknown_order", now)
                disposition = "unknown_order"
            else:
                agent, rid = order["agent_id"], order["request_id"]
                disposition = self._apply_event(c, order, status, amount, currency, verified,
                                                provider_capture_id or provider_order_id,
                                                provider, now)
                c.execute("INSERT OR REPLACE INTO provider_events (provider, event_id, "
                          "event_type, provider_order_id, status, amount, currency, verified, "
                          "raw_sha256, received_at, processed_at, disposition, agent_id, "
                          "request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (*values, disposition, agent, rid))
                self._write_outcome_audit(c, rid, agent, f"provider_event:{disposition}", now)
                return disposition, self._entry(c, order)
        raise UnknownProviderOrder(f"no order {provider_order_id} for provider {provider}")

    @staticmethod
    def _entry(c: sqlite3.Connection, order: sqlite3.Row) -> dict | None:
        row = c.execute("SELECT * FROM ledger_entries WHERE agent_id=? AND request_id=?",
                        (order["agent_id"], order["request_id"])).fetchone()
        return dict(row) if row else None

    _FINAL = {"completed": LedgerState.completed, "declined": LedgerState.failed,
              "refunded": LedgerState.reversed, "reversed": LedgerState.reversed}

    def _apply_event(self, c: sqlite3.Connection, order: sqlite3.Row, status: str,
                     amount: Decimal | None, currency: str | None, verified: bool,
                     reference: str, provider: str, now: datetime) -> str:
        agent, rid = order["agent_id"], order["request_id"]
        # Refunds reduce counted spending, while provider evidence always names gross terms.
        entry = c.execute("SELECT l.state, r.amount, l.currency FROM ledger_entries l "
                          "JOIN requests r ON r.agent_id=l.agent_id AND r.request_id=l.request_id "
                          "WHERE l.agent_id=? AND l.request_id=?", (agent, rid)).fetchone()
        state = LedgerState(entry["state"])
        target = self._FINAL.get(status)
        if (currency is not None and currency != entry["currency"]) or (
                amount is not None and Decimal(str(amount)) != Decimal(entry["amount"])) or (
                target is not None and (amount is None or currency is None)):
            return "mismatch_refused"
        if not verified:
            if state == LedgerState.pending:
                c.execute("UPDATE provider_orders SET recheck_requested=1, updated_at=? "
                          "WHERE agent_id=? AND request_id=?", (now.isoformat(), agent, rid))
            return "unverified_recorded"
        c.execute("UPDATE provider_orders SET last_status=?, updated_at=? WHERE agent_id=? "
                  "AND request_id=?", (status, now.isoformat(), agent, rid))
        method = f"provider_event:{provider}"
        if status == "unknown":
            if state in (LedgerState.pending, LedgerState.completed, LedgerState.failed,
                         LedgerState.cancelled, LedgerState.reversed):
                self._set_state(c, agent, rid, LedgerState.reconciliation_required,
                                f"verified:{method}", "provider_status_unknown", now)
            return "unknown_reconciliation"
        if target is None:  # verified but not final (created, pending, payer action...)
            if state in (LedgerState.pending, LedgerState.reconciliation_required):
                return "applied_non_final"
            return "out_of_order_ignored"
        if state in (LedgerState.pending, LedgerState.reconciliation_required):
            if target == LedgerState.reversed:  # refund seen before the completion
                completed = c.execute("SELECT 1 FROM payment_outcomes WHERE agent_id=? "
                                      "AND request_id=? AND outcome='completed' AND verified=1",
                                      (agent, rid)).fetchone()
                if state == LedgerState.reconciliation_required and completed:
                    self._set_state(c, agent, rid, LedgerState.reversed,
                                    f"verified:{method}", "payment_reversed", now)
                    return "applied"
                if state == LedgerState.pending:
                    self._set_state(c, agent, rid, LedgerState.reconciliation_required,
                                    f"verified:{method}", "refund_before_completion", now)
                return "conflict_reconciliation"
            self._set_state(c, agent, rid, target, f"verified:{method}",
                            f"payment_{target.value}", now)
            outcome = "completed" if target == LedgerState.completed else "failed"
            c.execute("INSERT INTO payment_outcomes (agent_id, request_id, outcome, verified, "
                      "verification_method, provider_reference, at) VALUES (?,?,?,1,?,?,?)",
                      (agent, rid, outcome, method, reference, now.isoformat()))
            return "applied"
        if state == target == LedgerState.completed:
            prior = c.execute("SELECT provider_reference FROM payment_outcomes WHERE "
                              "agent_id=? AND request_id=? AND verified=1 AND "
                              "outcome='completed' ORDER BY id DESC LIMIT 1",
                              (agent, rid)).fetchone()
            if prior and prior[0] != reference and reference != order["provider_order_id"] \
                    and prior[0] != order["provider_order_id"]:
                self._set_state(c, agent, rid, LedgerState.reconciliation_required,
                                f"verified:{method}", "conflicting_capture_reference", now)
                return "conflict_reconciliation"
        if state == target or (state == LedgerState.reversed
                               and target == LedgerState.completed) \
                or (state == LedgerState.cancelled and target == LedgerState.failed):
            return "out_of_order_ignored"  # already there, or a stale earlier status
        if state == LedgerState.completed and target == LedgerState.reversed:
            self._set_state(c, agent, rid, LedgerState.reversed, f"verified:{method}",
                            "payment_reversed", now)
            return "applied"
        if state in (LedgerState.completed, LedgerState.failed, LedgerState.cancelled):
            self._set_state(c, agent, rid, LedgerState.reconciliation_required,
                            f"verified:{method}", f"conflicting_{status}_after_{state.value}",
                            now)
            return "conflict_reconciliation"
        return "out_of_order_ignored"

    def provider_events(self, request_id: str | None = None, *,
                        agent_id: str | None = None) -> list[dict]:
        with self._lock:
            if request_id is None:
                rows = self._conn.execute("SELECT * FROM provider_events ORDER BY rowid")
            else:
                agent = self._agent_of(self._conn, request_id, agent_id)
                rows = self._conn.execute("SELECT * FROM provider_events WHERE agent_id=? "
                                          "AND request_id=? ORDER BY rowid", (agent, request_id))
            return [dict(r) for r in rows.fetchall()]

    def lifecycle_facts(self, request_id: str, *, agent_id: str | None = None) -> dict | None:
        """Everything needed to report a request as a PaymentLifecycle."""
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            req = self._conn.execute("SELECT status, recommendation FROM requests WHERE "
                                     "agent_id=? AND request_id=?",
                                     (agent, request_id)).fetchone()
            if req is None:
                return None
            entry = self._conn.execute("SELECT state, owner_id, expires_at FROM "
                                       "ledger_entries WHERE agent_id=? AND request_id=?",
                                       (agent, request_id)).fetchone()
            last = self._conn.execute("SELECT reason FROM ledger_events WHERE agent_id=? "
                                      "AND request_id=? ORDER BY id DESC LIMIT 1",
                                      (agent, request_id)).fetchone()
            order = self._conn.execute("SELECT provider_order_id FROM provider_orders WHERE "
                                       "agent_id=? AND request_id=?",
                                       (agent, request_id)).fetchone()
        return {"agent_id": agent, "request_id": request_id, "request_status": req["status"],
                "recommendation": req["recommendation"],
                "ledger_state": entry["state"] if entry else None,
                "owner_id": entry["owner_id"] if entry else None,
                "expires_at": entry["expires_at"] if entry else None,
                "last_reason": last["reason"] if last else None,
                "provider_order_id": order["provider_order_id"] if order else None}

    def record_payment_outcome(self, request_id: str, outcome: str, verified: bool,
                               verification_method: str, provider_reference: str,
                               now: datetime | None = None, *,
                               amount: Decimal | None = None, currency: str | None = None,
                               attempt_reference: str | None = None,
                               agent_id: str | None = None) -> None:
        """Record a payment outcome. Unverified outcomes are stored for audit but change
        nothing: the ledger stays `pending` and trust is not established.

        Hardening (SEC-08):
        * the request must have a ledger entry;
        * `amount`/`currency`, when given, must equal the attempted ones, and
          `attempt_reference`, when given, must name a recorded attempt. Phase 2 must pass
          all three; they are optional only so the Phase-1 harnesses keep working;
        * a verified outcome that repeats the settling one (same outcome and provider
          reference, e.g. a redelivered webhook) is a no-op audited as
          `outcome_duplicate_ignored`;
        * any other verified outcome for a settled entry raises `OutcomeConflict`.
        Every refusal changes nothing and is audited as `outcome_rejected:<reason>`.
        Provider webhooks go through `apply_provider_event` instead."""
        if outcome not in ("completed", "failed", "cancelled"):
            raise ValueError("outcome must be completed, failed or cancelled")
        now = _as_utc(now or self.clock())
        reason, agent = "invalid_transition", agent_id
        try:
            with self._tx() as c:
                if self.strict_execution and verified:
                    reason = "legacy_outcome_disabled"
                    raise LegacyPathDisabled("verified outcomes require a provider event")
                agent = self._agent_of(c, request_id, agent_id)
                entry = c.execute("SELECT state, amount, currency, agent_id FROM ledger_entries "
                                  "WHERE agent_id=? AND request_id=?",
                                  (agent, request_id)).fetchone()
                if entry is None:
                    known = c.execute("SELECT 1 FROM requests WHERE request_id=?",
                                      (request_id,)).fetchone()
                    reason = "no_ledger_entry" if known else "unknown_request"
                    raise InvalidLedgerTransition(f"no ledger entry for {request_id}")
                if (currency is not None and currency.strip().upper() != entry["currency"]) \
                        or (amount is not None
                            and Decimal(str(amount)) != Decimal(entry["amount"])):
                    reason = "amount_or_currency_mismatch"
                    raise OutcomeMismatch(
                        f"outcome amount/currency differ from the ledger entry of {request_id}")
                if attempt_reference is not None and c.execute(
                        "SELECT 1 FROM payment_attempts WHERE agent_id=? AND request_id=? AND "
                        "provider_reference=?",
                        (agent, request_id, attempt_reference)).fetchone() is None:
                    reason = "unknown_attempt_reference"
                    raise OutcomeMismatch(
                        f"no recorded attempt with that reference for {request_id}")
                if verified:
                    state = LedgerState(entry["state"])
                    if state in _SETTLED_STATES:
                        prior = c.execute(
                            "SELECT outcome, provider_reference FROM payment_outcomes WHERE "
                            "agent_id=? AND request_id=? AND verified=1 ORDER BY id LIMIT 1",
                            (agent, request_id)).fetchone()
                        if prior is not None and prior["outcome"] == outcome and \
                                prior["provider_reference"] == provider_reference:
                            self._write_outcome_audit(c, request_id, agent,
                                                      "outcome_duplicate_ignored", now)
                            return
                        reason = "conflicts_with_settled_outcome"
                        raise OutcomeConflict(
                            f"{request_id} is already settled as {state.value}")
                    self._set_state(c, agent, request_id, LedgerState(outcome),
                                    f"verified:{verification_method}", f"payment_{outcome}",
                                    now)
                c.execute("INSERT INTO payment_outcomes (agent_id, request_id, outcome, "
                          "verified, verification_method, provider_reference, at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (agent, request_id, outcome, int(verified), verification_method,
                           provider_reference, now.isoformat()))
        except (InvalidLedgerTransition, OutcomeMismatch) as exc:
            try:
                with self._tx() as c:
                    self._write_outcome_audit(c, request_id, agent,
                                              f"outcome_rejected:{reason}", now,
                                              [type(exc).__name__])
            except sqlite3.Error:
                pass  # the refusal itself must still reach the caller
            raise

    @staticmethod
    def _write_outcome_audit(c: sqlite3.Connection, request_id: str, agent_id: str | None,
                             status: str, now: datetime, errors: list[str] | None = None
                             ) -> None:
        c.execute("INSERT INTO audit_log (request_id, agent_id, timestamp, status, errors) "
                  "VALUES (?,?,?,?,?)", (request_id, agent_id, now.isoformat(), status,
                                         json.dumps(errors or [])))

    def trusted_history_for(self, agent_id: str, owner_id: str | None,
                            limit: int = 500) -> list[HistoricalPayment]:
        """Verified, completed payments of this agent (and owner) only."""
        if owner_id is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.* FROM requests r "
                "JOIN ledger_entries l ON l.agent_id = r.agent_id "
                "AND l.request_id = r.request_id "
                "WHERE r.agent_id = ? AND l.owner_id = ? AND l.state = 'completed' "
                "AND EXISTS (SELECT 1 FROM payment_outcomes o WHERE o.agent_id = r.agent_id "
                "            AND o.request_id = r.request_id "
                "            AND o.verified = 1 AND o.outcome = 'completed') "
                "ORDER BY r.received_at DESC LIMIT ?", (agent_id, owner_id, limit)).fetchall()
        return [self._row_to_history(r) for r in reversed(rows)]

    def shared_trusted_history_for(self, agent_id: str, owner_id: str,
                                   limit: int = 500) -> list[HistoricalPayment]:
        """Verified completed payments of the SAME owner's OTHER agents (decision 8). Only
        called when the owner explicitly enabled sharing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.* FROM requests r "
                "JOIN ledger_entries l ON l.agent_id = r.agent_id "
                "AND l.request_id = r.request_id "
                "WHERE r.agent_id != ? AND l.owner_id = ? AND l.state = 'completed' "
                "AND EXISTS (SELECT 1 FROM payment_outcomes o WHERE o.agent_id = r.agent_id "
                "            AND o.request_id = r.request_id "
                "            AND o.verified = 1 AND o.outcome = 'completed') "
                "ORDER BY r.received_at DESC LIMIT ?", (agent_id, owner_id, limit)).fetchall()
        return [self._row_to_history(r) for r in reversed(rows)]

    def trust_trail(self, request_id: str, *, agent_id: str | None = None) -> list[dict]:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            rows = self._conn.execute(
                "SELECT 'owner_decision' AS kind, decision AS detail, NULL AS verified, at "
                "FROM owner_decisions WHERE agent_id=:a AND request_id=:r UNION ALL "
                "SELECT 'payment_attempt', provider, NULL, at FROM payment_attempts "
                "WHERE agent_id=:a AND request_id=:r UNION ALL "
                "SELECT 'payment_outcome', outcome, verified, at FROM payment_outcomes "
                "WHERE agent_id=:a AND request_id=:r ORDER BY at",
                {"r": request_id, "a": agent}).fetchall()
        return [dict(r) for r in rows]

    def expire_holds(self, now: datetime | None = None) -> None:
        """Release lapsed reservations/approvals (and move overdue attempts to
        reconciliation_required) in their own committed transaction, so a later refused
        transition cannot roll the change back."""
        with self._tx() as c:
            self._expire_reservations(c, _as_utc(now or self.clock()))

    def recover_stale_claims(self, now: datetime | None = None,
                             older_than: timedelta = timedelta(minutes=5)) -> list[str]:
        """SEC-09: a worker that crashed between the claim and `complete_request` leaves the
        request `claimed`, with a reservation that holds budget until the request's own
        expiry (up to 48 h). Claims older than `older_than`, which must exceed the longest
        possible assessment, are marked `assessment_error` and their hold is released. The
        id stays claimed, so a retry still needs a new request_id (fail closed)."""
        now = _as_utc(now or self.clock())
        cutoff = (now - older_than).isoformat()
        with self._tx() as c:
            rows = c.execute("SELECT request_id, agent_id FROM requests WHERE status='claimed' "
                             "AND received_at < ?", (cutoff,)).fetchall()
            for r in rows:
                self._release_if_reserved(c, r["agent_id"], r["request_id"],
                                          "stale_claim_recovered", now)
                c.execute("UPDATE requests SET status='assessment_error', completed_at=? "
                          "WHERE agent_id=? AND request_id=? AND status='claimed'",
                          (now.isoformat(), r["agent_id"], r["request_id"]))
                self._write_outcome_audit(c, r["request_id"], r["agent_id"],
                                          "recovered_stale_claim", now, ["stale_claim"])
        return [r["request_id"] for r in rows]

    def spend_breakdown(self, agent_id: str, currency: str, at: datetime) -> dict[str, Decimal]:
        """Owner decision 4: amounts per ledger state for the budget period containing `at`.
        `attempted` = payment submitted, outcome not yet verified (state `pending`);
        `reconciliation_required` = provider outcome unknown, still held (SEC-16);
        `committed` = reserved + approved + attempted + reconciliation_required + completed
        (counts against budget)."""
        at = _as_utc(at)
        with self._tx() as c:
            self._expire_reservations(c, at)
            rows = c.execute("SELECT amount, state FROM ledger_entries WHERE agent_id=? "
                             "AND currency=? AND period=?",
                             (agent_id, currency, budget_period(at))).fetchall()
        names = {"reserved": "reserved", "approved": "approved", "pending": "attempted",
                 "completed": "completed", "failed": "failed", "cancelled": "cancelled",
                 "released": "released", "reconciliation_required": "reconciliation_required",
                 "reversed": "reversed"}
        out = {v: Decimal(0) for v in names.values()}
        for amount, state in rows:
            out[names[state]] += Decimal(amount)
        out["committed"] = (out["reserved"] + out["approved"] + out["attempted"]
                            + out["reconciliation_required"] + out["completed"])
        return out

    def complete_request(self, req: PaymentRequest, assessment: RiskAssessment) -> None:
        payload = json.dumps(_redact_obj(assessment.model_dump(mode="json")))
        with self._tx() as c:
            if assessment.recommendation == "block":
                self._release_if_reserved(c, req.agent_id, req.request_id,
                                          "assessment_blocked", assessment.assessed_at)
            c.execute(
                "UPDATE requests SET status='assessed', recommendation=?, assessment_json=?, "
                "effective_expires_at=?, completed_at=? WHERE agent_id=? AND request_id=? "
                "AND status='claimed'",
                (assessment.recommendation, payload, assessment.effective_expires_at.isoformat(),
                 self.clock().isoformat(), req.agent_id, req.request_id))
            self._write_audit(c, req.request_id, req.agent_id, "assessed", assessment)

    def fail_request(self, req: PaymentRequest, errors: list[str]) -> None:
        """A claimed request whose assessment crashed. The id stays claimed (fail
        closed): a retry must use a new request_id."""
        with self._tx() as c:
            self._release_if_reserved(c, req.agent_id, req.request_id, "assessment_error",
                                      self.clock())
            c.execute("UPDATE requests SET status='assessment_error', completed_at=? "
                      "WHERE agent_id=? AND request_id=? AND status='claimed'",
                      (self.clock().isoformat(), req.agent_id, req.request_id))
            self._write_audit(c, req.request_id, req.agent_id, "assessment_error", None,
                              errors)

    def _release_if_reserved(self, c: sqlite3.Connection, agent_id: str, request_id: str,
                             reason: str, now: datetime) -> None:
        row = c.execute("SELECT state FROM ledger_entries WHERE agent_id=? AND request_id=?",
                        (agent_id, request_id)).fetchone()
        if row and row[0] == LedgerState.reserved.value:
            self._set_state(c, agent_id, request_id, LedgerState.released, "system", reason,
                            now)

    def record_rejection(self, reason: str, request_id: str | None, agent_id: str | None,
                         details: list[dict]) -> None:
        """Security/audit event for a request rejected before assessment (422/413/400).
        Callers pass only validated identifiers and error locations/types, never raw
        request content."""
        with self._tx() as c:
            c.execute("INSERT INTO audit_log (request_id, agent_id, timestamp, status, errors) "
                      "VALUES (?,?,?,?,?)",
                      (request_id or "unknown", agent_id, self.clock().isoformat(),
                       f"rejected:{reason}", json.dumps(_redact_obj(details))))

    def record_replay(self, req: PaymentRequest, assessment: RiskAssessment) -> None:
        with self._tx() as c:
            self._write_audit(c, req.request_id, req.agent_id, "replay_rejected", assessment)

    # --------------------------------------------------------------- history
    def history_for(self, request_id: str, agent_id: str, merchant_name: str,
                    merchant_domain: str | None, limit: int = 1000) -> list[HistoricalPayment]:
        """Earlier observed requests relevant to a new one (same agent or same merchant),
        excluding every row with the request's id: ids are scoped per agent (SEC-13), so
        another agent's use of the same id is not a duplicate of this one."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE request_id != ? AND (agent_id=? "
                "OR lower(merchant_name)=lower(?) OR merchant_domain=?) "
                "ORDER BY received_at DESC LIMIT ?",
                (request_id, agent_id, merchant_name, merchant_domain, limit)).fetchall()
        return [self._row_to_history(r) for r in reversed(rows)]

    @staticmethod
    def _row_to_history(r: sqlite3.Row) -> HistoricalPayment:
        return HistoricalPayment(
            request_id=r["request_id"], agent_id=r["agent_id"],
            merchant_name=r["merchant_name"], merchant_domain=r["merchant_domain"],
            amount=Decimal(r["amount"]), currency=r["currency"],
            recurring=bool(r["recurring"]),
            created_at=datetime.fromisoformat(r["received_at"]),
            recommendation=r["recommendation"])

    # ----------------------------------------------------------------- audit
    def _write_audit(self, c: sqlite3.Connection, request_id: str, agent_id: str | None,
                     status: str, a: RiskAssessment | None,
                     errors: list[str] | None = None) -> None:
        c.execute(
            "INSERT INTO audit_log (request_id, agent_id, timestamp, status, model_backend, "
            "model_name, model_version, is_fallback, rules_version, risk_score, risk_category, "
            "recommendation, signal_codes, errors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, agent_id, (a.assessed_at if a else self.clock()).isoformat(), status,
             a.model.backend if a else None, a.model.model_name if a else None,
             a.model.model_version if a else None,
             int(a.model.is_fallback) if a else None, a.rules_version if a else None,
             a.risk_score if a else None, a.risk_category if a else None,
             a.recommendation if a else None,
             json.dumps([s.code for s in a.signals]) if a else None,
             json.dumps([redact(e) for e in (errors or (a.model.errors if a else []))])))

    # ----------------------------------------------------------------- reads
    def get_assessment(self, request_id: str, agent_id: str | None = None
                       ) -> RiskAssessment | None:
        """The stored assessment. Without `agent_id`, an id used by several agents returns
        None (fail closed, SEC-13): callers should pass the authenticated agent."""
        with self._lock:
            try:
                agent = self._agent_of(self._conn, request_id, agent_id)
            except AmbiguousRequestId:
                return None
            row = self._conn.execute(
                "SELECT assessment_json FROM requests WHERE agent_id=? AND request_id=? AND "
                "status='assessed'", (agent, request_id)).fetchone()
        return RiskAssessment.model_validate_json(row[0]) if row else None

    def received_at(self, request_id: str, *, agent_id: str | None = None) -> datetime | None:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            row = self._conn.execute("SELECT received_at FROM requests WHERE agent_id=? AND "
                                     "request_id=?", (agent, request_id)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def request_status(self, request_id: str, *, agent_id: str | None = None) -> str | None:
        with self._lock:
            agent = self._agent_of(self._conn, request_id, agent_id)
            row = self._conn.execute("SELECT status FROM requests WHERE agent_id=? AND "
                                     "request_id=?", (agent, request_id)).fetchone()
        return row[0] if row else None

    def audit_records(self, request_id: str | None = None) -> list[dict]:
        with self._lock:
            if request_id:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log WHERE request_id=? ORDER BY id", (request_id,))
            else:
                rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id")
            return [dict(r) for r in rows.fetchall()]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Read-only access for recovery and consistency checks."""
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("query() is read-only")
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Schema v3, kept verbatim so that v2/v3 files can be completed and verified before the
# v3 -> v4 rebuild (SEC-11, SEC-13).
_SCHEMA_V3 = [
    """CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        merchant_name TEXT NOT NULL,
        merchant_domain TEXT,
        merchant_domain_input TEXT,
        amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        recurring INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        client_created_at TEXT,
        client_expires_at TEXT,
        effective_expires_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('claimed','assessed','assessment_error')),
        recommendation TEXT,
        assessment_json TEXT,
        completed_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent_id, received_at)",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        agent_id TEXT, timestamp TEXT NOT NULL, status TEXT NOT NULL,
        model_backend TEXT, model_name TEXT, model_version TEXT,
        is_fallback INTEGER, rules_version TEXT, risk_score INTEGER,
        risk_category TEXT, recommendation TEXT, signal_codes TEXT,
        errors TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_audit_req ON audit_log(request_id)",
    """CREATE TABLE IF NOT EXISTS ledger_entries (
        request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
        agent_id TEXT NOT NULL,
        owner_id TEXT,
        merchant_key TEXT NOT NULL,
        currency TEXT NOT NULL,
        amount TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('reserved','approved','pending','completed',
                                             'released','failed','cancelled')),
        period TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ledger_agent ON ledger_entries(agent_id, currency, state)",
    """CREATE TABLE IF NOT EXISTS owner_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('approved','declined')),
        decided_by TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payment_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        provider TEXT NOT NULL, provider_reference TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payment_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('completed','failed','cancelled')),
        verified INTEGER NOT NULL CHECK (verified IN (0,1)),
        verification_method TEXT NOT NULL, provider_reference TEXT NOT NULL,
        at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS request_terms (
        request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
        terms_sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ledger_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL,
        from_state TEXT, to_state TEXT NOT NULL,
        actor TEXT NOT NULL, reason TEXT, at TEXT NOT NULL)""",
]

_SCHEMA = [
    # SEC-13: (agent_id, request_id) is the key; uniqueness = per-agent replay protection
    """CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        merchant_name TEXT NOT NULL,
        merchant_domain TEXT,                    -- normalized (punycode, lowercase)
        merchant_domain_input TEXT,              -- original text, redacted (audit only)
        amount TEXT NOT NULL,                    -- exact decimal string
        currency TEXT NOT NULL,
        recurring INTEGER NOT NULL,
        received_at TEXT NOT NULL,               -- trusted server receipt time (UTC)
        client_created_at TEXT,                  -- untrusted agent metadata
        client_expires_at TEXT,                  -- untrusted agent metadata
        effective_expires_at TEXT,               -- server-computed (P0.3)
        status TEXT NOT NULL CHECK (status IN ('claimed','assessed','assessment_error')),
        recommendation TEXT,
        assessment_json TEXT,
        completed_at TEXT,
        PRIMARY KEY (agent_id, request_id))""",
    "CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent_id, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_requests_rid ON requests(request_id)",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        agent_id TEXT, timestamp TEXT NOT NULL, status TEXT NOT NULL,
        model_backend TEXT, model_name TEXT, model_version TEXT,
        is_fallback INTEGER, rules_version TEXT, risk_score INTEGER,
        risk_category TEXT, recommendation TEXT, signal_codes TEXT,
        errors TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_audit_req ON audit_log(request_id)",
    # One ledger entry per request. Amounts are exact decimal strings; each entry records
    # its own currency and nothing is ever converted (P0.2 / P0.5).
    """CREATE TABLE IF NOT EXISTS ledger_entries (
        agent_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        owner_id TEXT,
        merchant_key TEXT NOT NULL,
        currency TEXT NOT NULL,
        amount TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('reserved','approved','pending','completed',
                                             'released','failed','cancelled',
                                             'reconciliation_required','reversed')),
        period TEXT NOT NULL,                    -- budget period (YYYY-MM, UTC)
        created_at TEXT NOT NULL,                -- server time
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,                -- hold / approval / attempt deadline
        PRIMARY KEY (agent_id, request_id),
        FOREIGN KEY (agent_id, request_id) REFERENCES requests(agent_id, request_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ledger_agent ON ledger_entries(agent_id, currency, state)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_state ON ledger_entries(state, expires_at)",
    # Append-only trust records (P0.4). Nothing here is ever updated or deleted.
    # owner_id/session_id/auth_method: the authenticated identity (SEC-15); NULL only for
    # legacy free-text decisions, which can never be executed.
    """CREATE TABLE IF NOT EXISTS owner_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('approved','declined')),
        decided_by TEXT NOT NULL,
        owner_id TEXT, session_id TEXT, auth_method TEXT,
        at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payment_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        provider TEXT NOT NULL, provider_reference TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payment_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('completed','failed','cancelled')),
        verified INTEGER NOT NULL CHECK (verified IN (0,1)),
        verification_method TEXT NOT NULL, provider_reference TEXT NOT NULL,
        at TEXT NOT NULL)""",
    # SHA-256 of the canonical approval terms, written once in the claim transaction
    # (final owner decision 5). The terms themselves are not stored.
    """CREATE TABLE IF NOT EXISTS request_terms (
        agent_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        terms_sha256 TEXT NOT NULL,
        PRIMARY KEY (agent_id, request_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_events (     -- append-only state history
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        from_state TEXT, to_state TEXT NOT NULL,
        actor TEXT NOT NULL, reason TEXT, at TEXT NOT NULL)""",
    # SEC-15: structured, time-limited, single-use owner approvals. At most one per request
    # (the ledger can be approved once). Only consumed_at/consumed_by may ever be written
    # after insert, and only once.
    """CREATE TABLE IF NOT EXISTS approvals (
        approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, org_id TEXT,
        session_id TEXT NOT NULL, auth_method TEXT NOT NULL,
        mfa_satisfied INTEGER NOT NULL CHECK (mfa_satisfied IN (0,1)),
        terms_sha256 TEXT NOT NULL,
        approved_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        consumed_at TEXT, consumed_by TEXT,
        UNIQUE (agent_id, request_id),
        CHECK (owner_id != agent_id))""",
    """CREATE TRIGGER IF NOT EXISTS trg_approvals_single_use BEFORE UPDATE ON approvals
        WHEN OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
          OR NEW.consumed_by IS NULL OR length(NEW.consumed_by)=0
          OR NEW.approval_id IS NOT OLD.approval_id OR NEW.org_id IS NOT OLD.org_id
          OR NEW.auth_method IS NOT OLD.auth_method
          OR NEW.mfa_satisfied IS NOT OLD.mfa_satisfied
          OR NEW.approved_at IS NOT OLD.approved_at
          OR NEW.terms_sha256 IS NOT OLD.terms_sha256 OR NEW.owner_id IS NOT OLD.owner_id
          OR NEW.session_id IS NOT OLD.session_id OR NEW.expires_at IS NOT OLD.expires_at
          OR NEW.agent_id IS NOT OLD.agent_id OR NEW.request_id IS NOT OLD.request_id
        BEGIN SELECT RAISE(ABORT, 'approvals are single-use and immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_approvals_no_delete BEFORE DELETE ON approvals
        BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END""",
    # SEC-16/SEC-20: one provider order per attempt; both idempotency keys are written in
    # the execution transaction, before any provider call.
    """CREATE TABLE IF NOT EXISTS provider_orders (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_order_id TEXT,
        create_key TEXT NOT NULL UNIQUE,
        capture_key TEXT NOT NULL UNIQUE,
        last_status TEXT NOT NULL,
        recheck_requested INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, request_id),
        CHECK (create_key != capture_key))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_orders_ref
        ON provider_orders(provider, provider_order_id)""",
    """CREATE TABLE IF NOT EXISTS provider_operations (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('create_order','capture')),
        idempotency_key TEXT NOT NULL UNIQUE, invoked_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, request_id, operation))""",
    # every provider event ever received, deduplicated by (provider, event_id)
    """CREATE TABLE IF NOT EXISTS provider_events (
        provider TEXT NOT NULL, event_id TEXT NOT NULL,
        event_type TEXT NOT NULL, provider_order_id TEXT NOT NULL,
        status TEXT NOT NULL, amount TEXT, currency TEXT,
        verified INTEGER NOT NULL CHECK (verified IN (0,1)),
        raw_sha256 TEXT NOT NULL, received_at TEXT NOT NULL, processed_at TEXT NOT NULL,
        disposition TEXT NOT NULL,
        agent_id TEXT, request_id TEXT,
        PRIMARY KEY (provider, event_id))""",
    # Additive v4 table: preserve capture identity without rewriting existing event rows.
    """CREATE TABLE IF NOT EXISTS provider_event_captures (
        provider TEXT NOT NULL, event_id TEXT NOT NULL, provider_capture_id TEXT,
        PRIMARY KEY (provider, event_id))""",
    """CREATE INDEX IF NOT EXISTS idx_provider_events_unknown
        ON provider_events(disposition, received_at)""",
    """CREATE TABLE IF NOT EXISTS provider_order_links (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL, payer_action_url TEXT,
        PRIMARY KEY (agent_id, request_id))""",
    """CREATE TABLE IF NOT EXISTS provider_capture_checks (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        event_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
        amount TEXT NOT NULL, currency TEXT NOT NULL,
        observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, request_id))""",
]
