"""Wallet persistence (SQLite): owners, agents, capability grants, payment-instrument
references, wallet-prepared payment requests, owner approvals made through the wallet,
local sandbox simulations, and the permission audit; and, since schema v2, the developer
platform's tables (`platform_` prefix: agent profiles, hashed agent API keys, virtual
wallets, payment metadata and idempotency records; see agentpal/platform).

These are NEW tables with a `wallet_` prefix. The wallet never writes the assessment
store's tables (`requests`, `ledger_entries`, ...) directly; it changes ledger state only
through `AssessmentStore.record_owner_decision` / `record_payment_attempt`. It never touches
`PRAGMA user_version` (owned by agentpal.store); its own schema version lives in
`wallet_meta`. By default it uses its own database (in memory, or a separate file), so it
cannot contend for the assessment store's write lock.

Grant history, approvals, simulations and the permission audit are append-only: the
store has no method that updates or deletes them.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from ..private_files import ensure_private_db

WALLET_SCHEMA_VERSION = 2  # v2: platform_* tables (developer API, agentpal.platform)


class WalletStoreError(RuntimeError):
    pass


class WalletStore:
    def __init__(self, path: str | Path = ":memory:", busy_timeout_s: float = 30.0):
        self.path = str(path)
        self._lock = threading.RLock()
        self._tx_sequence = 0
        ensure_private_db(str(self.path))
        self._conn = sqlite3.connect(self.path, timeout=busy_timeout_s,
                                     isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            # The platform holds an outer transaction across preparation and its
            # idempotent response. Registry/wallet operations compose within it;
            # releasing a savepoint must never commit the outer transaction.
            nested = self._conn.in_transaction
            self._tx_sequence += 1
            savepoint = f"wallet_nested_{self._tx_sequence}"
            self._conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                if nested:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise WalletStoreError("query() is read-only")
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _migrate(self) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS wallet_meta (key TEXT PRIMARY KEY, "
                      "value TEXT NOT NULL)")
            row = c.execute("SELECT value FROM wallet_meta WHERE key='schema_version'"
                            ).fetchone()
            if row is not None and int(row[0]) > WALLET_SCHEMA_VERSION:
                raise WalletStoreError(f"{self.path} has newer wallet schema v{row[0]}")
            for stmt in _SCHEMA:
                c.execute(stmt)
            counter_columns = {r[1] for r in c.execute("PRAGMA table_info(platform_counter_offers)")}
            if "deal_index" not in counter_columns:
                c.execute("ALTER TABLE platform_counter_offers ADD COLUMN deal_index INTEGER")
            # Additive column (E2E-T1-004): why a refund definitively failed, for the owner.
            columns = {r[1] for r in c.execute("PRAGMA table_info(platform_paypal_refunds)")}
            if "failure_reason" not in columns:
                c.execute("ALTER TABLE platform_paypal_refunds ADD COLUMN failure_reason TEXT")
            c.execute("INSERT OR REPLACE INTO wallet_meta (key, value) VALUES "
                      "('schema_version', ?)", (str(WALLET_SCHEMA_VERSION),))

    def audit_permission(self, *, agent_id: str, owner_id: str | None, capability: str,
                         allowed: bool, code: str, action: str, at: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO wallet_permission_audit (agent_id, owner_id, capability, "
                      "allowed, code, action, at) VALUES (?,?,?,?,?,?,?)",
                      (agent_id, owner_id, capability, int(allowed), code, action[:120],
                       at.isoformat()))

    def permission_audit(self, agent_id: str | None = None) -> list[dict]:
        if agent_id is None:
            return self.query("SELECT * FROM wallet_permission_audit ORDER BY id")
        return self.query("SELECT * FROM wallet_permission_audit WHERE agent_id=? ORDER BY id",
                          (agent_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS wallet_owners (
        owner_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS wallet_agents (
        agent_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES wallet_owners(owner_id),
        role TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','suspended')),
        created_at TEXT NOT NULL)""",
    # current grants; every change is also in wallet_grant_events
    """CREATE TABLE IF NOT EXISTS wallet_grants (
        agent_id TEXT NOT NULL REFERENCES wallet_agents(agent_id),
        capability TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        granted_by TEXT NOT NULL,
        granted_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, capability))""",
    """CREATE TABLE IF NOT EXISTS wallet_grant_events (      -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('register','grant','revoke','suspend',
                                               'reactivate','refused')),
        capability TEXT, actor TEXT NOT NULL, detail TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS wallet_instruments (
        instrument_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES wallet_owners(owner_id),
        kind TEXT NOT NULL CHECK (kind IN ('virtual_allowance',
                                           'provider_sandbox_instrument')),
        provider TEXT NOT NULL CHECK (provider IN ('paypal_sandbox','local_simulation')),
        external_ref TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    # payment requests built by WalletService.prepare_payment; the canonical terms are kept
    # so the owner can review exactly what they approve (the assessment store keeps only
    # their digest)
    """CREATE TABLE IF NOT EXISTS wallet_payment_requests (
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        terms_json TEXT NOT NULL, terms_sha256 TEXT NOT NULL,
        recommendation TEXT NOT NULL, risk_score INTEGER NOT NULL,
        created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_wpr_agent ON wallet_payment_requests(agent_id)",
    """CREATE TABLE IF NOT EXISTS wallet_owner_decisions (    -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL, owner_id TEXT NOT NULL, token_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('approved','declined','refused')),
        terms_sha256 TEXT, detail TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS wallet_sandbox_simulations (  -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE, agent_id TEXT NOT NULL,
        simulation_reference TEXT NOT NULL CHECK (simulation_reference LIKE 'sim-%'),
        terms_sha256 TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS wallet_permission_audit (    -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, owner_id TEXT, capability TEXT NOT NULL,
        allowed INTEGER NOT NULL CHECK (allowed IN (0,1)),
        code TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL)""",
    # ---- v2: developer platform (agentpal.platform). Additive only: a v1 file upgrades
    # in place because every statement is CREATE ... IF NOT EXISTS.
    """CREATE TABLE IF NOT EXISTS platform_agent_profiles (
        agent_id TEXT PRIMARY KEY REFERENCES wallet_agents(agent_id),
        name TEXT NOT NULL, description TEXT NOT NULL, metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    # API keys: only the SHA-256 of the full key is stored, never the key or its secret
    """CREATE TABLE IF NOT EXISTS platform_agent_keys (
        key_id TEXT PRIMARY KEY CHECK (length(key_id) = 16),
        agent_id TEXT NOT NULL REFERENCES wallet_agents(agent_id),
        owner_id TEXT NOT NULL,
        key_sha256 TEXT NOT NULL CHECK (length(key_sha256) = 64),
        created_at TEXT NOT NULL, created_by TEXT NOT NULL,
        expires_at TEXT, revoked_at TEXT, revoked_reason TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_pak_agent ON platform_agent_keys(agent_id)",
    """CREATE TABLE IF NOT EXISTS platform_credential_events (   -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL, key_id TEXT,
        action TEXT NOT NULL CHECK (action IN ('issue','rotate','revoke','overlap_expiry',
                                               'refused')),
        actor TEXT NOT NULL, detail TEXT, at TEXT NOT NULL)""",
    # virtual allowance configuration: limits only. There is no balance column; every
    # figure shown to a caller is derived from the ledger.
    """CREATE TABLE IF NOT EXISTS platform_wallets (
        wallet_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        agent_id TEXT NOT NULL REFERENCES wallet_agents(agent_id),
        currency TEXT NOT NULL,
        monthly_budget TEXT NOT NULL,
        per_transaction_hard_limit TEXT NOT NULL,
        per_transaction_review_limit TEXT NOT NULL,
        rolling_window_limit TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','frozen')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (agent_id, currency))""",
    "CREATE INDEX IF NOT EXISTS idx_pw_owner ON platform_wallets(owner_id)",
    """CREATE TABLE IF NOT EXISTS platform_wallet_events (       -- append-only
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('create','update','freeze','unfreeze',
                                               'refused')),
        actor TEXT NOT NULL, before_json TEXT, after_json TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform_payment_meta (
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL, wallet_id TEXT NOT NULL,
        decision_requested INTEGER NOT NULL CHECK (decision_requested IN (0,1)),
        decision_requested_at TEXT, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ppm_owner ON platform_payment_meta(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_ppm_agent ON platform_payment_meta(agent_id)",
    """CREATE TABLE IF NOT EXISTS platform_market_checks (   -- Channel3 advisory evidence
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        idem_key TEXT, body_sha256 TEXT,      -- reuse only for a retry of the same request
        product_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    # Lead 2026-11-06 (UI-T2 LOW-2): what the agent said, shown to the OWNER as advisory text.
    """CREATE TABLE IF NOT EXISTS platform_agent_context (
        request_id TEXT PRIMARY KEY, business_justification TEXT NOT NULL,
        product_query TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform_page_checks (
        request_id TEXT PRIMARY KEY, product_url TEXT, payload_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform_idempotency (
        agent_id TEXT NOT NULL, idem_key TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('in_progress','done')),
        request_id TEXT, response_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, idem_key))""",
    # P3: metadata is a projection; the existing assessment ledger remains the authority
    # for reservations, approvals, attempts, verified outcomes and allowance totals.
    # Audit only: identical capture/GET event fingerprints are deduplicated by the
    # financial ledger. Preserve successful authenticated GET provenance separately.
    """CREATE TABLE IF NOT EXISTS platform_paypal_read_receipts (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        event_id TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY(agent_id, request_id, event_id))""",
    """CREATE TABLE IF NOT EXISTS platform_payout_requests (
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL, wallet_id TEXT NOT NULL,
        idem_key TEXT NOT NULL, body_sha256 TEXT NOT NULL,
        terms_json TEXT NOT NULL, terms_sha256 TEXT NOT NULL,
        response_json TEXT, created_at TEXT NOT NULL,
        UNIQUE (agent_id, idem_key))""",
    """CREATE TABLE IF NOT EXISTS platform_payout_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT, agent_id TEXT NOT NULL, actor TEXT NOT NULL,
        code TEXT NOT NULL, item_status TEXT, raw_sha256 TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform_paypal_refunds (
        refund_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
        key_sha256 TEXT NOT NULL, body_sha256 TEXT NOT NULL, provider_key TEXT NOT NULL UNIQUE,
        capture_id TEXT NOT NULL, order_id TEXT NOT NULL,
        amount TEXT NOT NULL, currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('COMPLETED','PENDING','FAILED','CANCELLED',
                                              'reconciliation_required')),
        provider_refund_id TEXT UNIQUE, evidence_sha256 TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(request_id,key_sha256))""",
    """CREATE TABLE IF NOT EXISTS platform_paypal_refund_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, refund_id TEXT NOT NULL,
        action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL)""",
    # Owner counter-offer ("Buy this instead"). One counter per original is a database
    # fact (primary key). A row is written BEFORE the original is declined, with a NULL
    # replacement, so an interrupted counter can only be resumed with the same terms; the
    # link is shown only once `replacement_request_id` is set. No existing table changes.
    """CREATE TABLE IF NOT EXISTS platform_counter_offers (
        original_request_id TEXT PRIMARY KEY,
        replacement_request_id TEXT UNIQUE,
        owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        original_amount TEXT NOT NULL, replacement_amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('manual','channel3_deal')),
        body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
        created_at TEXT NOT NULL)""",

    """CREATE TABLE IF NOT EXISTS platform_savers (
        saver_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        agent_id TEXT NOT NULL UNIQUE REFERENCES wallet_agents(agent_id),
        status TEXT NOT NULL CHECK(status IN ('active','retired')),
        created_at TEXT NOT NULL, retired_at TEXT, retirement_reason TEXT,
        note TEXT NOT NULL, note_source TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS platform_saver_one_active
        ON platform_savers(owner_id) WHERE status='active'""",
    """CREATE TABLE IF NOT EXISTS platform_saver_suggestions (
        suggestion_id TEXT PRIMARY KEY, saver_id TEXT NOT NULL REFERENCES platform_savers(saver_id),
        owner_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        agent_id TEXT NOT NULL, deal_index INTEGER NOT NULL,
        retailer TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL, saving TEXT NOT NULL,
        body_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform_saver_periods (
        saver_id TEXT NOT NULL REFERENCES platform_savers(saver_id), period TEXT NOT NULL,
        report_json TEXT NOT NULL, evaluated_at TEXT NOT NULL,
        PRIMARY KEY(saver_id,period))""",
    """CREATE TABLE IF NOT EXISTS platform_saver_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, saver_id TEXT NOT NULL,
        action TEXT NOT NULL, at TEXT NOT NULL)""",
]
