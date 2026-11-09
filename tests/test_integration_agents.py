"""Lead integration test: Agent 1's REAL intelligence code (CompositeProductExtractor,
assess_sources, report_from_assessment) wired into Agent 2's REAL workforce (Orchestrator,
HttpFetchTool with a mocked transport, registry-backed permissions, wallet). Fully offline.

The pages are the synthetic fixtures in tests/fixtures/intel/ (not real merchants)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx

from agentpal.contracts import Provenance, SourceEvidence
from agentpal.intelligence.adapter import build_report
from agentpal.intelligence.extractors import CompositeProductExtractor
from agentpal.intelligence.research import assess_sources
from agentpal.schemas import PaymentRequest
from agentpal.spending import LedgerState
from agentpal.tools import EvidenceStore, HttpFetchTool
from agentpal.workforce.orchestrator import Orchestrator, StepStatus, TaskStatus

from test_wallet_fixtures import build_world
from test_workforce_orchestrator import TEAM, intent

FIX = Path(__file__).resolve().parent / "fixtures" / "intel"


def _setup(tmp_path, page: str):
    w = build_world()
    store = EvidenceStore(tmp_path / "evidence")
    body = (FIX / page).read_bytes()

    def handler(request):
        if (request.headers["host"], request.url.path) == ("example.com", "/pro"):
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    fetch = HttpFetchTool(w.checker, store, transport=httpx.MockTransport(handler),
                          resolver=lambda h, p: ["93.184.215.14"] if h == "example.com"
                          else (_ for _ in ()).throw(OSError()), clock=w.clock)
    orch = Orchestrator(w.registry, w.checker, w.wallet, store, fetch_tool=fetch,
                        extractors=[CompositeProductExtractor()],
                        source_assessor=assess_sources)
    return w, orch, body


def test_real_extractor_and_assessor_feed_the_workforce_and_the_gate_holds(tmp_path):
    w, orch, body = _setup(tmp_path, "jsonld_product.html")
    r = orch.run_purchase_research(intent(), TEAM)

    # the price the shopping agent proposes was OBSERVED by Agent 1's extractor on the
    # fetched bytes (not the 999.99 in an unrelated script on the same page)
    assert r.proposal.amount == Decimal("49.00")
    assert r.proposal.amount_provenance == Provenance.observed
    assert r.proposal.amount_source.content_sha256 == SourceEvidence.digest(body)
    assert all(f.extracted_by for f in r.facts)

    # the advisory seam ran with the real assessor: facts + discrepancies, no conflict
    facts, discrepancies = r.advisory
    assert any(f.name == "price" and f.value == Decimal("49.00") for f in facts)
    assert all(d.severity != "review" for d in discrepancies)  # page agrees with the terms
    assert any(s.action == "intelligence.assess_sources" and s.status == StepStatus.done
               for s in r.steps)

    # advisory output changes nothing: the gate holds, the hold stays reserved
    assert r.status == TaskStatus.awaiting_owner_approval and r.execution is None
    assert w.risk.store.ledger_state(r.prepared.request_id) == LedgerState.reserved
    assert orch.resume_after_approval(r).status == TaskStatus.awaiting_owner_approval


def test_structured_report_from_the_workforce_run_is_advisory(tmp_path):
    w, orch, _ = _setup(tmp_path, "jsonld_product.html")
    r = orch.run_purchase_research(intent(), TEAM)
    facts, discrepancies = r.advisory
    req_terms = r.prepared.terms
    req = PaymentRequest.model_validate({
        "request_id": req_terms.request_id, "agent_id": req_terms.agent_id,
        "amount": str(req_terms.amount), "currency": req_terms.currency,
        "merchant_name": req_terms.merchant_name,
        "merchant_domain": req_terms.merchant_domain, "purpose": req_terms.purpose,
        "business_justification": "Hosting for a customer project",
        "recurring": True, "billing_interval": "monthly"})
    rep = build_report(req, rule_signals=[], observations=facts,
                       discrepancies=discrepancies)
    assert rep.authoritative is False and rep.payment_executed is False
    assert rep.advisory != "blocked_by_rules"  # only a rule hard violation can block


def test_conflicting_observed_prices_halt_the_proposal(tmp_path):
    w, orch, _ = _setup(tmp_path, "conflicting_sources.html")
    r = orch.run_purchase_research(intent(), TEAM)
    assert r.proposal is None and r.prepared is None
    assert r.status != TaskStatus.awaiting_owner_approval
    assert "conflicting observed prices" in (r.stop_reason or "")


def test_injected_page_text_is_data_and_obeys_nothing(tmp_path):
    w, orch, _ = _setup(tmp_path, "injection_text.html")
    r = orch.run_purchase_research(intent(), TEAM)
    # the page's text is at most an observed product_name; nothing is approved
    assert r.execution is None
    if r.prepared is not None:
        assert r.status == TaskStatus.awaiting_owner_approval
        assert w.risk.store.ledger_state(r.prepared.request_id) == LedgerState.reserved
        assert w.wallet.owner_decisions(r.prepared.request_id) == []
