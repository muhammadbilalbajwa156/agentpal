"""Render SDK v1 flow registration. Optional SDK is imported only at startup."""

import json
import uuid

from .core import STOP, Shopper, ShopperError

_process_nonce: str | None = None


def planning_execution() -> str:
    """Record a process identity BEFORE starting a bounded, non-retriable LLM step."""
    global _process_nonce
    if _process_nonce is None:
        _process_nonce = uuid.uuid4().hex
    return _process_nonce


def _with_shopper(method, *args, **kwargs):
    shopper = Shopper(emit=lambda event: print(json.dumps(event), flush=True))
    try:
        return getattr(shopper, method)(*args, **kwargs)
    finally:
        shopper.close()


def plan_and_search(task: str, budget: str, execution_nonce: str) -> dict:
    # An interrupted planning step must not restart six more model calls on replay.
    # Completed planning is reused by Render; incomplete planning fails closed.
    if execution_nonce != planning_execution():
        raise ShopperError("interrupted_planning_cannot_be_replayed")
    shopper = Shopper(emit=lambda event: print(json.dumps(event), flush=True))
    try:
        return {"body": shopper.plan(task, budget), "scope": shopper.identity_scope}
    finally:
        shopper.close()


def request_payment(plan: dict, errand_id: str) -> dict:
    return _with_shopper("request_payment", plan["body"], errand_id, expected_scope=plan["scope"])


def wait_for_owner_decision(request_id: str) -> dict:
    return _with_shopper("wait", request_id, owner=True, timeout=21600)


def execute(request_id: str) -> dict:
    return _with_shopper("execute", request_id)


def wait_for_provider(request_id: str) -> dict:
    return _with_shopper("wait", request_id, owner=False, timeout=21600)


def shop_errand(ctx, task: str, budget: str) -> dict:
    # Stable run identity; I/O stays INSIDE steps so replay is deterministic.
    errand_id = ctx.metadata.flow_run_id
    execution_nonce = ctx.step("planning_execution", planning_execution)
    plan = ctx.step("plan_and_search", plan_and_search, task, budget, execution_nonce)
    request = ctx.step("request_payment", request_payment, plan, errand_id)
    request_id = request["request_id"]
    decision = ctx.step("wait_for_owner_decision", wait_for_owner_decision, request_id)
    request_id = decision["request_id"]
    if decision["status"] != "approved":
        return {"errand_id": errand_id, "request_id": request_id, "status": decision["status"]}
    result = ctx.step("execute", execute, request_id)
    if result["status"] not in STOP:
        result = ctx.step("wait_for_provider", wait_for_provider, request_id)
    return {"errand_id": errand_id, "request_id": request_id, "status": result["status"]}


def create_workflows():
    try:
        from render import Workflows
    except ImportError:
        raise ShopperError("install_optional_render_sdk_v1") from None
    from ..company.render_flow import company_day
    app = Workflows()
    app.flow(name="shop_errand", timeout_seconds=86400)(shop_errand)
    # Autonomous companies: same deployed workflow (agentpal-shopper), same start module.
    app.flow(name="company_day", timeout_seconds=7200)(company_day)
    return app


if __name__ == "__main__":
    create_workflows().start()
