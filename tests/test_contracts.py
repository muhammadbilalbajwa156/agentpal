"""Shared intelligence/workforce contracts: provenance can never be faked by construction."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentpal.contracts import (AgentContext, Capability, ObservedFact, Provenance,
                                SourceEvidence, ToolError, ToolResult)

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SRC = SourceEvidence(url="https://shop.example/item", retrieved_at=NOW,
                     content_sha256=SourceEvidence.digest(b"<html></html>"), method="http_fetch")


def test_observed_fact_requires_a_source_and_inferred_must_not_cite_one():
    ObservedFact(name="price", value=Decimal("49.00"), unit="USD",
                 provenance=Provenance.observed, source=SRC, extracted_by="jsonld")
    with pytest.raises(ValidationError, match="cite the source"):
        ObservedFact(name="price", value="49", provenance=Provenance.observed,
                     extracted_by="model")
    with pytest.raises(ValidationError, match="pose as sourced"):
        ObservedFact(name="price", value="49", provenance=Provenance.inferred, source=SRC,
                     extracted_by="model")
    with pytest.raises(ValidationError, match="no value"):
        ObservedFact(name="price", value="49", provenance=Provenance.unavailable,
                     extracted_by="tool")


def test_failed_tool_cannot_report_facts():
    fact = ObservedFact(name="price", value="1", provenance=Provenance.observed, source=SRC,
                        extracted_by="x")
    with pytest.raises(ValidationError, match="cannot report facts"):
        ToolResult(tool="fetch", ok=False, error=ToolError.timeout, facts=[fact],
                   started_at=NOW, finished_at=NOW)
    with pytest.raises(ValidationError, match="say why"):
        ToolResult(tool="fetch", ok=False, started_at=NOW, finished_at=NOW)


def test_source_evidence_rules():
    with pytest.raises(ValidationError):
        SourceEvidence(url="file:///etc/passwd", retrieved_at=NOW,
                       content_sha256="0" * 64, method="http_fetch")
    with pytest.raises(ValidationError, match="timezone"):
        SourceEvidence(url="https://a.example", retrieved_at=datetime(2026, 1, 1),
                       content_sha256="0" * 64, method="http_fetch")


def test_agent_context_is_immutable_and_scoped():
    ctx = AgentContext(agent_id="agent_research_1", owner_id="owner_a", role="research",
                       capabilities=frozenset({Capability.research_fetch}))
    with pytest.raises(ValidationError):
        ctx.agent_id = "other"  # frozen
    assert Capability.payment_execute_sandbox not in ctx.capabilities


def test_agent_context_ids_follow_the_request_id_pattern():
    """Any agent id valid in a payment request can hold a context (dots and colons too)."""
    ctx = AgentContext(agent_id="agent.demo:001", owner_id="owner.demo", role="research")
    assert ctx.agent_id == "agent.demo:001"
    with pytest.raises(ValidationError):
        AgentContext(agent_id="ab", owner_id="owner_a", role="research")  # too short
