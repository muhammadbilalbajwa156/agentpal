"""Process isolation of approvals, provider invocation and API idempotency claims."""
import multiprocessing as mp
import os
from pathlib import Path

import pytest

from agentpal.lifecycle import PaymentLifecycleService
from agentpal.lifecycle.recovery import startup_recovery
from agentpal.spending import ApprovalTerms, InvalidLedgerTransition, LedgerState
from agentpal.store import AssessmentStore
from test_lifecycle_execution import FakeProvider, setup
from test_sec_v2_isolation import T0, make, req


def _race(path, operation, ticket_json, ready, start, output):
    from agentpal.lifecycle.service import ExecutionTicket
    store = AssessmentStore(path, strict_execution=True)
    store.clock = lambda: T0
    lifecycle = PaymentLifecycleService(store)
    ticket = ExecutionTicket.model_validate_json(ticket_json) if ticket_json else None
    provider = FakeProvider(store, ticket)
    ready.put(True)
    start.wait(15)
    try:
        if operation == "begin":
            request = req("agent_a", amount="40.00")
            lifecycle.begin_execution(request.agent_id, request.request_id,
                                      ApprovalTerms.from_request(request), "fake")
        elif operation == "submit":
            lifecycle.submit_to_provider(ticket, provider)
        else:
            lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
        output.put((True, provider.creates, provider.captures))
    except InvalidLedgerTransition:
        output.put((False, provider.creates, provider.captures))
    finally:
        store.close()


def processes_run(target, arguments):
    ctx = mp.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=target, args=(*arguments, ready, start, output)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=30)
        start.set()
        results = [output.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        return results
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
        ready.close()
        output.close()


@pytest.mark.parametrize("operation", ["begin", "submit", "capture"])
def test_independent_processes_consume_and_invoke_once(tmp_path, operation):
    path = str(tmp_path / "ledger.db")
    if operation == "begin":
        risk = make(path)
        request = req("agent_a", amount="40.00")
        risk.assess(request)
        risk.store.approve_authenticated(request.request_id, ApprovalTerms.from_request(request),
            agent_id=request.agent_id, owner_id="owner_a", session_id="session_test",
            auth_method="owner_api_token", now=T0)
        store, ticket_json = risk.store, None
    else:
        store, lifecycle, ticket, provider = setup(path)
        if operation == "capture":
            lifecycle.submit_to_provider(ticket, provider)
        ticket_json = ticket.model_dump_json()
    results = processes_run(_race, (path, operation, ticket_json))
    assert sum(result[0] for result in results) == 1
    assert sum(result[1] for result in results) == (operation == "submit")
    assert sum(result[2] for result in results) == (operation == "capture")
    approvals = store.query("SELECT * FROM approvals")
    assert len(approvals) == 1
    assert approvals[0]["consumed_at"] is not None
    assert len(store.query("SELECT * FROM payment_attempts")) == 1
    store.close()


def _crash_in_provider(path, ticket_json, marker):
    from agentpal.lifecycle.service import ExecutionTicket
    store = AssessmentStore(path, strict_execution=True)
    store.clock = lambda: T0
    ticket = ExecutionTicket.model_validate_json(ticket_json)
    class CrashingProvider(FakeProvider):
        def create_order(self, *args, **kwargs):
            Path(marker).write_text("provider received invocation")
            os._exit(9)
    PaymentLifecycleService(store).submit_to_provider(ticket, CrashingProvider(store, ticket))


def test_process_death_after_provider_receives_call_never_retries_or_releases(tmp_path):
    path, marker = str(tmp_path / "ledger.db"), str(tmp_path / "provider-received")
    store, _, ticket, _ = setup(path)
    store.close()
    process = mp.get_context("spawn").Process(target=_crash_in_provider,
                                               args=(path, ticket.model_dump_json(), marker))
    process.start()
    process.join(20)
    try:
        assert process.exitcode == 9 and Path(marker).exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
    store = AssessmentStore(path, strict_execution=True)
    store.clock = lambda: T0
    report = startup_recovery(store, now=T0)
    assert report.clean
    assert store.ledger_state(ticket.request_id) == LedgerState.reconciliation_required
    provider = FakeProvider(store, ticket)
    with pytest.raises(InvalidLedgerTransition):
        PaymentLifecycleService(store).submit_to_provider(ticket, provider)
    assert provider.creates == 0
    before = store.ledger_events(ticket.request_id)
    startup_recovery(store, now=T0)
    assert store.ledger_events(ticket.request_id) == before
    assert store.spend_breakdown(ticket.agent_id, "USD", T0)["committed"] == ticket.terms.amount
    store.close()


def _idempotent_http(path, key, ready, start, output):
    from agentpal.api import create_app
    from agentpal.platform.wiring import build_platform
    from fastapi.testclient import TestClient
    from test_infra_integration_platform import settings_for, bearer
    settings = settings_for(path)
    deps = build_platform(settings=settings, environ={})
    app = create_app(service=deps.risk_service, settings=settings, platform=deps)
    try:
        with TestClient(app) as client:
            ready.put(True)
            start.wait(15)
            response = client.post("/v1/payment-requests", headers={**bearer(key),
                "Idempotency-Key": "multi-process-idem"}, json={"amount": "1.00", "currency": "USD",
                "merchant_name": "Example", "purpose": "Concurrent offline request"})
            output.put(response.status_code)
    finally:
        deps.close()


def test_http_idempotency_across_processes_reserves_once(tmp_path):
    from agentpal.api import create_app
    from agentpal.platform.wiring import build_platform
    from fastapi.testclient import TestClient
    from test_infra_integration_platform import settings_for, bearer
    path = str(tmp_path / "risk.db")
    settings = settings_for(path)
    deps = build_platform(settings=settings, environ={})
    deps.owner_authenticator.create_owner("owner_parallel")
    token = deps.owner_authenticator.issue_token("owner_parallel")[0]
    app = create_app(service=deps.risk_service, settings=settings, platform=deps)
    with TestClient(app) as client:
        agent = client.post("/v1/agents", headers=bearer(token), json={"name": "Parallel bot",
            "capabilities": ["payment.prepare", "owner.request_decision"]}).json()
        assert client.post("/v1/wallets", headers=bearer(token), json={
            "agent_id": agent["agent"]["agent_id"], "currency": "USD", "monthly_budget": "100.00",
            "per_transaction_limit": "10.00"}).status_code == 201
    deps.close()
    statuses = processes_run(_idempotent_http, (path, agent["api_key"]))
    assert statuses.count(201) == 1
    assert all(status in (200, 201, 409) for status in statuses)
    store = AssessmentStore(path)
    assert len(store.query("SELECT * FROM ledger_entries")) == 1
    assert str(store.spend_breakdown(agent["agent"]["agent_id"], "USD", store.clock())["committed"]) == "1.00"
    store.close()
