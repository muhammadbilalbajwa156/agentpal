"""Virtual wallets: owner-configured spending limits for one agent in one currency.

A wallet stores LIMITS only. Available / reserved / pending / spent are derived from the
authoritative ledger (`AssessmentStore.spend_breakdown`) every time they are read; there is
no balance column anywhere. All of it is a virtual allowance, never money.

Owner limits are hard-capped by the operator (`PlatformLimits`). A frozen wallet is
omitted from the spend policy, so the deterministic reservation fails closed for it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from ..schemas import CURRENCY_MINOR_UNITS
from ..spending import CurrencyLimits, budget_period
from ..wallet.store import WalletStore
from .deps import PlatformLimits
from .errors import PlatformError, not_found

_LIMIT_FIELDS = ("monthly_budget", "per_transaction_hard_limit",
                 "per_transaction_review_limit", "rolling_window_limit")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _snapshot(row: dict) -> str:
    return json.dumps({k: row[k] for k in (*_LIMIT_FIELDS, "status")}, sort_keys=True)


class WalletBook:
    def __init__(self, store: WalletStore, limits: PlatformLimits,
                 clock: Callable[[], datetime], resources=None):
        self.store = store
        self.limits = limits
        self.clock = clock
        self.resources = resources

    # ----------------------------------------------------------- validation
    def _check(self, currency: str, values: dict[str, Decimal]) -> None:
        cap = self.limits.caps.get(currency)
        minor = CURRENCY_MINOR_UNITS.get(currency)
        if cap is None or minor is None:
            raise PlatformError(400, "currency_not_enabled")
        for v in values.values():
            if v <= 0 or v.as_tuple().exponent < -minor:
                raise PlatformError(400, "invalid_amount")
        monthly = values["monthly_budget"]
        hard = values["per_transaction_hard_limit"]
        if monthly > cap.max_monthly_budget or hard > cap.max_per_transaction \
                or values["rolling_window_limit"] > cap.max_monthly_budget:
            raise PlatformError(400, "above_operator_cap")
        if hard > monthly or values["per_transaction_review_limit"] > hard:
            raise PlatformError(400, "inconsistent_limits")

    # ----------------------------------------------------------------- CRUD
    def create(self, owner_id: str, actor: str, agent_id: str, currency: str,
               monthly: Decimal, hard: Decimal, review: Decimal | None,
               rolling: Decimal | None) -> dict:
        values = {"monthly_budget": monthly, "per_transaction_hard_limit": hard,
                  "per_transaction_review_limit": review if review is not None else hard,
                  "rolling_window_limit": rolling if rolling is not None else monthly}
        if self.resources:
            self.resources.check_wallet(owner_id, values)
        self._check(currency, values)
        now = _iso(self.clock())
        wallet_id = "wal_" + uuid.uuid4().hex
        with self.store.tx() as c:
            agent = c.execute("SELECT owner_id FROM wallet_agents WHERE agent_id=?",
                              (agent_id,)).fetchone()
            if agent is None or agent["owner_id"] != owner_id:
                raise not_found("cross_owner_wallet_agent" if agent else None)
            count = c.execute("SELECT COUNT(*) FROM platform_wallets WHERE owner_id=?",
                              (owner_id,)).fetchone()[0]
            if count >= self.limits.max_wallets_per_owner:
                raise PlatformError(409, "wallet_limit_reached")
            if c.execute("SELECT 1 FROM platform_wallets WHERE agent_id=? AND currency=?",
                         (agent_id, currency)).fetchone():
                raise PlatformError(409, "wallet_exists")
            c.execute("INSERT INTO platform_wallets (wallet_id, owner_id, agent_id, currency, "
                      "monthly_budget, per_transaction_hard_limit, "
                      "per_transaction_review_limit, rolling_window_limit, status, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,'active',?,?)",
                      (wallet_id, owner_id, agent_id, currency,
                       *(str(values[k]) for k in _LIMIT_FIELDS), now, now))
            row = dict(c.execute("SELECT * FROM platform_wallets WHERE wallet_id=?",
                                 (wallet_id,)).fetchone())
            self._event(c, wallet_id, owner_id, "create", actor, None, _snapshot(row), now)
        return row

    def get(self, wallet_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM platform_wallets WHERE wallet_id=?",
                                (wallet_id,))
        return rows[0] if rows else None

    def get_owned(self, owner_id: str, wallet_id: str, actor: str) -> dict:
        row = self.get(wallet_id)
        if row is None:
            raise not_found()
        if row["owner_id"] != owner_id:
            with self.store.tx() as c:
                self._event(c, wallet_id, owner_id, "refused", actor, None, "cross_owner",
                            _iso(self.clock()))
            raise not_found("cross_owner_wallet")
        return row

    def list_owned(self, owner_id: str) -> list[dict]:
        return self.store.query("SELECT * FROM platform_wallets WHERE owner_id=? "
                                "ORDER BY created_at, wallet_id", (owner_id,))

    def for_agent(self, agent_id: str, currency: str | None = None) -> list[dict]:
        if currency is None:
            return self.store.query("SELECT * FROM platform_wallets WHERE agent_id=? "
                                    "ORDER BY currency", (agent_id,))
        return self.store.query("SELECT * FROM platform_wallets WHERE agent_id=? AND "
                                "currency=?", (agent_id, currency))

    def update(self, owner_id: str, actor: str, wallet_id: str,
               changes: dict[str, Decimal]) -> dict:
        row = self.get_owned(owner_id, wallet_id, actor)
        values = {k: Decimal(row[k]) for k in _LIMIT_FIELDS}
        values.update(changes)
        if self.resources:
            self.resources.check_wallet(owner_id, values)
        self._check(row["currency"], values)
        now = _iso(self.clock())
        with self.store.tx() as c:
            c.execute("UPDATE platform_wallets SET monthly_budget=?, "
                      "per_transaction_hard_limit=?, per_transaction_review_limit=?, "
                      "rolling_window_limit=?, updated_at=? WHERE wallet_id=?",
                      (*(str(values[k]) for k in _LIMIT_FIELDS), now, wallet_id))
            new = dict(c.execute("SELECT * FROM platform_wallets WHERE wallet_id=?",
                                 (wallet_id,)).fetchone())
            self._event(c, wallet_id, owner_id, "update", actor, _snapshot(row),
                        _snapshot(new), now)
        return new

    def set_status(self, owner_id: str, actor: str, wallet_id: str, status: str) -> dict:
        row = self.get_owned(owner_id, wallet_id, actor)
        now = _iso(self.clock())
        with self.store.tx() as c:
            c.execute("UPDATE platform_wallets SET status=?, updated_at=? WHERE wallet_id=?",
                      (status, now, wallet_id))
            new = dict(c.execute("SELECT * FROM platform_wallets WHERE wallet_id=?",
                                 (wallet_id,)).fetchone())
            self._event(c, wallet_id, owner_id,
                        "freeze" if status == "frozen" else "unfreeze", actor,
                        _snapshot(row), _snapshot(new), now)
        return new

    def events(self, wallet_id: str) -> list[dict]:
        return self.store.query("SELECT * FROM platform_wallet_events WHERE wallet_id=? "
                                "ORDER BY id", (wallet_id,))

    # ------------------------------------------------------ spend policy feed
    def is_platform_agent(self, agent_id: str) -> bool:
        return bool(self.store.query("SELECT 1 FROM platform_agent_profiles WHERE "
                                     "agent_id=?", (agent_id,)))

    def active_limits(self, agent_id: str) -> dict[str, CurrencyLimits]:
        """Limits of the agent's ACTIVE wallets (frozen ones are left out: fail closed)."""
        out = {}
        for r in self.for_agent(agent_id):
            if r["status"] != "active":
                continue
            cap = self.limits.caps.get(r["currency"])
            if cap is None:
                continue
            monthly = min(Decimal(r["monthly_budget"]), cap.max_monthly_budget)
            hard = min(Decimal(r["per_transaction_hard_limit"]), cap.max_per_transaction,
                       monthly)
            rolling = min(Decimal(r["rolling_window_limit"]), cap.max_monthly_budget)
            if self.resources and self.resources.amount_limits(r["owner_id"]):
                monthly = min(monthly, self.resources.max_monthly)
                hard = min(hard, self.resources.max_amount, monthly)
                rolling = min(rolling, self.resources.max_monthly)
            out[r["currency"]] = CurrencyLimits(
                monthly_budget=monthly,
                rolling_window_limit=rolling,
                per_transaction_hard_limit=hard,
                per_transaction_review_limit=min(Decimal(r["per_transaction_review_limit"]),
                                                 hard))
        return out

    # ---------------------------------------------------------- derived views
    @staticmethod
    def allowance(row: dict, ledger, now: datetime) -> dict:
        b = ledger.spend_breakdown(row["agent_id"], row["currency"], now)
        monthly = Decimal(row["monthly_budget"])
        q = Decimal(1).scaleb(-CURRENCY_MINOR_UNITS[row["currency"]])
        figures = {"available": max(Decimal(0), monthly - b["committed"]),
                   "reserved": b["reserved"] + b["approved"],
                   # E2E-T4-3: an uncertain outcome (reconciliation_required) is still held
                   # money awaiting PayPal; show it as pending so the figures add up.
                   "pending": b["attempted"] + b["reconciliation_required"],
                   "spent": b["completed"],
                   "committed": b["committed"]}
        return {"period": budget_period(now),
                **{k: v.quantize(q) for k, v in figures.items()}}

    @staticmethod
    def limits_view(row: dict) -> dict:
        return {"monthly_budget": Decimal(row["monthly_budget"]),
                "per_transaction_limit": Decimal(row["per_transaction_hard_limit"]),
                "per_transaction_review_limit": Decimal(row["per_transaction_review_limit"]),
                "rolling_window_limit": Decimal(row["rolling_window_limit"])}

    def transactions(self, agent_id: str, currency: str | None, ledger) -> list[dict]:
        rows = self.store.query("SELECT request_id, terms_json, created_at FROM "
                                "wallet_payment_requests WHERE agent_id=? "
                                "ORDER BY created_at, request_id", (agent_id,))
        out = []
        for r in rows:
            terms = json.loads(r["terms_json"])
            if currency is not None and terms["currency"] != currency:
                continue
            entry = ledger.ledger_entry(r["request_id"])
            out.append({
                "request_id": r["request_id"], "amount": Decimal(str(terms["amount"])),
                "currency": terms["currency"], "merchant_name": terms["merchant_name"],
                "purpose": terms["purpose"], "created_at": r["created_at"],
                "ledger_state": entry["state"] if entry else None,
                "events": [{"from_state": e["from_state"], "to_state": e["to_state"],
                            "reason": e["reason"], "at": e["at"]}
                           for e in ledger.ledger_events(r["request_id"])]})
        return out

    @staticmethod
    def _event(c, wallet_id: str, owner_id: str, action: str, actor: str,
               before: str | None, after: str | None, now: str) -> None:
        c.execute("INSERT INTO platform_wallet_events (wallet_id, owner_id, action, actor, "
                  "before_json, after_json, at) VALUES (?,?,?,?,?,?,?)",
                  (wallet_id, owner_id, action, actor, before, after, now))
