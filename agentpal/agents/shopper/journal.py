"""Private local checkpoints. One frozen body and logical payment per errand ID."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .core import STOP, Shopper, ShopperError, identifier, money, text


class Journal:
    def __init__(self, path: str, table: str = "shopper_errands"):
        self._table = identifier(table)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute(f"CREATE TABLE IF NOT EXISTS {self._table} "
                         "(id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def close(self):
        self._db.close()

    def update(self, errand_id: str, fn):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(f"SELECT state FROM {self._table} WHERE id=?",
                                       (errand_id,)).fetchone()
                state = json.loads(row[0]) if row else {}
                fn(state)
                self._db.execute(f"INSERT OR REPLACE INTO {self._table} VALUES (?,?)",
                                 (errand_id, json.dumps(state)))
                self._db.execute("COMMIT")
                return state
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def load(self, errand_id: str):
        return self.update(errand_id, lambda state: None)


def run_errand(shopper: Shopper, journal: Journal, task: str, budget: str, *,
               errand_id: str | None = None, merchant_paypal_email: str | None = None,
               wait_seconds: float = 3600) -> dict:
    errand_id = identifier(shopper.redact(errand_id or uuid.uuid4().hex))
    task, budget = text(shopper.redact(task), 500), money(budget)
    if merchant_paypal_email:
        merchant_paypal_email = shopper.redact(merchant_paypal_email)
    signature = {"task": task, "budget": budget, "payee": merchant_paypal_email,
                 "scope": shopper.identity_scope}

    def initialize(state):
        if state and state.get("input") != signature:
            raise ShopperError("errand_id_conflict")
        state.setdefault("input", signature)

    state = journal.update(errand_id, initialize)
    shopper.event("Shopping errand ready. Keep this ID to resume safely.", errand_id=errand_id)
    if not state.get("plan"):
        def claim_plan(state):
            if state.get("planning_started"):
                raise ShopperError("planning_already_attempted_start_a_new_errand")
            state["planning_started"] = True
        journal.update(errand_id, claim_plan)
        plan = shopper.plan(task, budget, merchant_paypal_email)
        state = journal.update(errand_id, lambda state: state.update(plan=plan))
    if not state.get("request_id"):
        # An ambiguous create is replayed with the SAME stored body and key.
        result = shopper.request_payment(state["plan"], errand_id,
                                         expected_scope=signature["scope"])
        state = journal.update(errand_id, lambda state: state.update(request_id=result["request_id"]))
    request_id = state["request_id"]
    view = shopper.wait(request_id, owner=True, timeout=wait_seconds)
    # Keep the original checkpoint as the chain anchor on resume; execute/report the
    # request actually returned by wait, which may be an owner's replacement.
    request_id = view["request_id"]
    if view["status"] == "approved":
        claimed = False

        def claim_execution(state):
            nonlocal claimed
            if not state.get("execution_started"):
                state["execution_started"] = True
                claimed = True
        journal.update(errand_id, claim_execution)
        if claimed:
            view = shopper.execute(request_id)
        else:
            shopper.event("Execution was already attempted. The owner must inspect any uncertain "
                          "outcome in the dashboard; this runner will not retry execution.")
            return {"errand_id": errand_id, "request_id": request_id, "status": view["status"]}
    if view["status"] not in STOP and view["status"] not in {"prepared", "awaiting_approval"}:
        view = shopper.wait(request_id, owner=False, timeout=wait_seconds)
    return {"errand_id": errand_id, "request_id": request_id, "status": view["status"]}
