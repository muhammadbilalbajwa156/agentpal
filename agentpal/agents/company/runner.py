"""Run a company's business day: each agent's errands, journaled so reruns are idempotent."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..common import STOP, AgentError, RemoteRefused, identifier
from ..shopper.journal import Journal
from .operator import Operator, errand_key
from .scenarios import COMPANIES, AgentSpec, Company, Errand

KEY = re.compile(r"apk_[A-Za-z0-9_-]{8,200}\Z")


def business_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_agent_keys(env: dict[str, str]) -> dict[str, str]:
    """Agent keys by slug (e.g. ``northstar-atlas``) from COMPANY_AGENT_KEYS (JSON object)
    or the private file named by COMPANY_AGENT_KEYS_FILE (written by ``provision``)."""
    raw = env.get("COMPANY_AGENT_KEYS", "")
    path = env.get("COMPANY_AGENT_KEYS_FILE", "")
    if not raw and path:
        try:
            if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise AgentError("agent_keys_file_must_be_private_0600")
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            raise AgentError("agent_keys_file_unreadable") from None
    if not raw:
        return {}
    try:
        keys = json.loads(raw)
        if not isinstance(keys, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and KEY.fullmatch(v)
                for k, v in keys.items()):
            raise ValueError
    except ValueError:
        raise AgentError("invalid_agent_keys_owner_credentials_refused") from None
    return keys


def run_errand(op: Operator, journal: Journal, errand: Errand, day: str, *,
               earlier: list[dict] | None = None, wait_seconds: float = 0,
               replan: bool = False) -> dict:
    company, agent = op.company.company_id, op.agent.key
    key = errand_key(company, agent, errand.errand_id, day)
    base = {"company": company, "agent": agent, "errand": errand.errand_id, "kind": errand.kind}
    signature = {**base, "day": day, "scope": op.scope, "catalogue": op.catalogue.version}

    def initialize(state):
        if state and state.get("input") != signature:
            raise AgentError("errand_conflict_different_agent_origin_or_catalogue")
        state.setdefault("input", signature)

    state = journal.update(key, initialize)
    if state.get("request_id"):
        op.event("Errand already requested today; reporting status without new model calls.",
                 errand=errand.errand_id)
    elif not state.get("plan"):
        if op.dry_run:
            plan = op.plan(errand, earlier)
            op.event("Dry run: this request would be sent; nothing was requested.",
                     planned_request=plan["body"])
            return {**base, "status": "dry_run_planned", "amount": plan["body"]["amount"]}

        def claim_plan(state):
            if state.get("planning_started") and not replan:
                raise AgentError("planning_already_attempted_rerun_with_replan")
            state["planning_started"] = True
        journal.update(key, claim_plan)
        plan = op.plan(errand, earlier)
        state = journal.update(key, lambda s: s.update(plan=plan))
    if not state.get("request_id"):
        if op.dry_run:
            return {**base, "status": "dry_run_planned", "amount": state["plan"]["body"]["amount"]}
        # An ambiguous create is replayed with the SAME frozen body and derived key.
        create = op.request_payment if state["plan"]["kind"] == "payment" else op.request_payout
        result = create(state["plan"]["body"], key, expected_scope=signature["scope"])
        state = journal.update(key, lambda s: s.update(request_id=result["request_id"],
                                                       status=result["status"]))
    request_id = state["request_id"]
    amount = state["plan"]["body"]["amount"]
    status = state.get("status", "awaiting_approval")
    if errand.kind == "payment" and not op.dry_run:
        view = (op.shop.wait(request_id, owner=True, timeout=wait_seconds) if wait_seconds > 0
                else op.shop.read(request_id))
        status = view["status"]
        if status == "approved" and wait_seconds > 0:
            claimed = []
            journal.update(key, lambda s: claimed.append(not s.get("execution_started"))
                           or s.update(execution_started=True))
            if not claimed[0]:
                op.event("Execution was already attempted; inspect it in the owner dashboard. "
                         "This runner never retries execution.")
            else:
                status = op.shop.execute(request_id)["status"]
                if status not in STOP:
                    status = op.shop.wait(request_id, owner=False, timeout=wait_seconds)["status"]
    elif errand.kind == "payout" and wait_seconds > 0 and not op.dry_run:
        status = _execute_payout_once(op, journal, key, request_id, status)
    journal.update(key, lambda s: s.update(status=status))
    return {**base, "request_id": request_id, "status": status, "amount": amount}


def _execute_payout_once(op, journal, key, request_id, status):
    """Agents cannot read payout status (owner-only), so --wait tries ONE execution: AgentPal
    refuses it unless the owner approved the exact terms. Ambiguous attempts are never retried."""
    state = journal.load(key)
    if state.get("payout_execution_started"):
        op.event("Payout execution was already attempted; check it in the owner dashboard.")
        return status
    journal.update(key, lambda s: s.update(payout_execution_started=True))
    try:
        return op.execute_payout(request_id)["status"]
    except RemoteRefused as exc:
        if exc.status == 409:  # definitive: not approved yet, nothing was sent to PayPal
            journal.update(key, lambda s: s.update(payout_execution_started=False))
            op.event("The owner has not approved this payout yet; rerun later with --wait.")
            return status
        raise


def run_company(company: Company | str, *, env: dict[str, str] | None = None,
                journal_path: str = ":memory:", day: str | None = None,
                agents: list[str] | None = None, dry_run: bool = False, wait_seconds: float = 0,
                replan: bool = False, planner: str = "llm",
                emit: Callable[[dict], None] = lambda event: None,
                operator_factory: Callable[..., Operator] = Operator) -> dict:
    env = dict(os.environ if env is None else env)
    company = COMPANIES[company] if isinstance(company, str) else company
    day = identifier(day or business_day())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise AgentError("invalid_business_day")
    chosen: list[AgentSpec] = [company.agent(a) for a in agents] if agents else list(company.agents)
    keys = load_agent_keys(env)
    # A dry run never touches the durable journal, so it cannot block or alter a real day.
    journal = Journal(":memory:" if dry_run else journal_path, table="company_errands")
    emit({"message": f"{company.name} business day {day} begins.", "company": company.name,
          "dry_run": dry_run, "planner": planner})
    results = []
    try:
        for spec in chosen:
            label = company.agentpal_name(spec)
            try:
                op = operator_factory(company, spec, agent_key=keys.get(company.slug(spec), ""),
                                      env=env, emit=emit, planner=planner, dry_run=dry_run)
            except AgentError as exc:
                emit({"agent": label, "message": "Agent skipped safely.", "error": str(exc)})
                results += [{"company": company.company_id, "agent": spec.key,
                             "errand": e.errand_id, "kind": e.kind, "status": "skipped",
                             "error": str(exc)} for e in spec.errands]
                continue
            earlier: list[dict] = []
            try:
                for errand in spec.errands:
                    try:
                        result = run_errand(op, journal, errand, day, earlier=earlier,
                                            wait_seconds=wait_seconds, replan=replan)
                    except AgentError as exc:
                        op.event("Errand stopped safely; nothing further was requested.",
                                 errand=errand.errand_id, error=str(exc))
                        result = {"company": company.company_id, "agent": spec.key,
                                  "errand": errand.errand_id, "kind": errand.kind,
                                  "status": "stopped_safely", "error": str(exc)}
                    results.append(result)
                    earlier.append(result)
            finally:
                op.close()
    finally:
        journal.close()
    emit({"message": f"{company.name} business day {day} ended.", "company": company.name,
          "summary": [{k: r.get(k) for k in ("agent", "errand", "status", "request_id")}
                      for r in results]})
    return {"company": company.company_id, "day": day, "results": results}
