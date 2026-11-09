"""Render Workflows (Python SDK v1) flow ``company_day``: each errand is checkpointed steps.

Registered by ``agentpal.agents.shopper.render_workflow.create_workflows`` (the deployed
``agentpal-shopper`` workflow's start module). The Render SDK is never imported here.

Per errand, in a fixed order (deterministic on replay). In SDK v1 a step that raises ends
the whole run even if caught, so steps RETURN a "stopped_safely" value instead:
  ``<agent>.<errand>.claim_planning`` -> ``.plan`` -> ``.request``
The claim records this process's nonce BEFORE the bounded model loop. If Render replays the run
on a new instance after an interruption inside ``.plan``, the recorded nonce no longer matches
and the step returns stopped_safely: no second model loop, no request. ``.request`` re-sends the frozen
body with the Idempotency-Key derived from company+agent+errand+day, so a replayed create
returns AgentPal's stored response instead of a second request.
"""

from __future__ import annotations

import json
import os

from ..common import AgentError
from ..shopper.render_workflow import planning_execution
from .operator import Operator, errand_key
from .runner import business_day, load_agent_keys
from .scenarios import COMPANIES


def _operator(company: str, agent: str, planner: str) -> Operator:
    env = dict(os.environ)
    comp = COMPANIES[company]
    spec = comp.agent(agent)
    return Operator(comp, spec, agent_key=load_agent_keys(env).get(comp.slug(spec), ""),
                    env=env, planner=planner,
                    emit=lambda event: print(json.dumps(event), flush=True))


def _stopped(agent: str, errand: str, code: str) -> dict:
    return {"agent": agent, "errand": errand, "status": "stopped_safely", "error": code}


def plan_step(company: str, agent: str, errand: str, day: str, planner: str,
              execution_nonce: str) -> dict:
    # Render SDK v1: a step that RAISES ends the whole run even if the flow catches it, so a
    # safe stop is returned as a recorded value and only this errand stops.
    if execution_nonce != planning_execution():
        return _stopped(agent, errand, "interrupted_planning_cannot_be_replayed")
    op = None
    try:
        op = _operator(company, agent, planner)
        spec = COMPANIES[company].agent(agent)
        chosen = next(e for e in spec.errands if e.errand_id == errand)
        return {**op.plan(chosen), "scope": op.scope}
    except AgentError as exc:
        return _stopped(agent, errand, str(exc))
    finally:
        if op:
            op.close()


def request_step(company: str, agent: str, errand: str, day: str, plan: dict) -> dict:
    op = None
    try:
        op = _operator(company, agent, "scripted")  # no model is used to create the request
        key = errand_key(company, agent, errand, day)
        create = op.request_payment if plan["kind"] == "payment" else op.request_payout
        result = create(plan["body"], key, expected_scope=plan["scope"])
        return {"agent": agent, "errand": errand, "kind": plan["kind"],
                "request_id": result["request_id"], "status": result["status"],
                "amount": plan["body"]["amount"]}
    except AgentError as exc:
        # An ambiguous create is safe to re-run: same day => same Idempotency-Key and body.
        return _stopped(agent, errand, str(exc))
    finally:
        if op:
            op.close()


def company_day(ctx, company: str, planner: str = "llm") -> dict:
    if company not in COMPANIES or planner not in {"llm", "scripted"}:
        raise AgentError("unknown_company_or_planner")
    day = ctx.step("business_day", business_day)  # time read inside a step: replay-safe
    results = []
    for spec in COMPANIES[company].agents:
        for errand in spec.errands:
            prefix = f"{spec.key}.{errand.errand_id}"
            nonce = ctx.step(prefix + ".claim_planning", planning_execution)
            plan = ctx.step(prefix + ".plan", plan_step, company, spec.key,
                            errand.errand_id, day, planner, nonce)
            if plan.get("status") == "stopped_safely":
                results.append(plan)  # deterministic: the recorded value decides the branch
                continue
            results.append(ctx.step(prefix + ".request", request_step, company, spec.key,
                                    errand.errand_id, day, plan))
    return {"company": company, "day": day, "results": results}
