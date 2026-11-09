"""Shared contracts between AgentPal's intelligence engine and its agent workforce.

Owned by the CTO/lead. The intelligence engine (agentpal/intelligence/) and the agent
wallet + workforce (agentpal/wallet/, agentpal/workforce/, agentpal/tools/) both import
these types; neither changes them without review.

Core rule: information carries its PROVENANCE. A value an agent or a document merely
claims is never presented as observed, and an observed value is never presented as
verified truth. Nothing here moves money or grants authority.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import ID_PATTERN


class Provenance(str, Enum):
    """Where a piece of information came from (weakest to strongest is NOT implied)."""

    observed = "observed"        # extracted by a tool from a retrieved source (URL + time + hash)
    claimed = "claimed"          # asserted by an agent, a request, a document or a message
    inferred = "inferred"        # produced by a model or a heuristic; never a fact
    unavailable = "unavailable"  # looked for and not found / tool failed


class Capability(str, Enum):
    """Permission vocabulary. Granted per agent by the owner (agentpal/wallet). Holding a
    capability never bypasses deterministic spending policy or human approval."""

    research_search = "research.search"              # run web searches
    research_fetch = "research.fetch"                # fetch pages from allowed origins
    evidence_record = "evidence.record"              # store evidence with provenance
    intelligence_assess = "intelligence.assess"      # request a risk/intelligence report
    purchase_propose = "purchase.propose"            # create a purchase proposal (no money)
    wallet_read = "wallet.read"                      # read own wallet, budgets, history
    finance_monitor = "finance.monitor"              # read owner-scoped financial activity
    comms_draft = "comms.draft"                      # draft email/WhatsApp messages
    comms_send_approved = "comms.send_approved"      # send ONLY owner-approved messages
    payment_prepare = "payment.prepare"              # build a payment request for approval
    owner_request_decision = "owner.request_decision"  # ask the owner to decide; never decides
    payment_execute_sandbox = "payment.execute_sandbox"  # sandbox/simulation execution only
    payout_request = "payout.request"                # ask the owner to approve a payout; never sends


class SourceEvidence(BaseModel):
    """A retrieved source. Retrieval proves only that these bytes were served at that URL
    at that time; it does not make the content true or the site legitimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str = Field(min_length=8, max_length=2048)
    retrieved_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599)
    method: str = Field(pattern=r"^(http_fetch|browser|search_result|fixture)$")
    final_url: str | None = Field(default=None, max_length=2048)  # after redirects

    @field_validator("url", "final_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError("source URLs must be http(s)")
        return v

    @field_validator("retrieved_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @staticmethod
    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


class ObservedFact(BaseModel):
    """One piece of information with its provenance, e.g. price=49.00 USD observed on a
    product page. `source` is required for observed facts and forbidden for inferred ones."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")        # price, currency, availability...
    value: str | Decimal | bool | None
    unit: str | None = Field(default=None, max_length=16)       # ISO currency, "days", ...
    provenance: Provenance
    source: SourceEvidence | None = None
    extracted_by: str = Field(min_length=1, max_length=64)      # tool / extractor / model id
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _provenance_rules(self) -> "ObservedFact":
        if self.provenance == Provenance.observed and self.source is None:
            raise ValueError("an observed fact must cite the source it was extracted from")
        if self.provenance == Provenance.inferred and self.source is not None:
            raise ValueError("an inferred fact must not pose as sourced")
        if self.provenance == Provenance.unavailable and self.value is not None:
            raise ValueError("an unavailable fact has no value")
        return self


class ToolError(str, Enum):
    not_permitted = "not_permitted"          # capability not granted / origin not allowed
    blocked_target = "blocked_target"        # private network, non-http scheme, bad port
    timeout = "timeout"
    too_large = "too_large"
    http_error = "http_error"
    not_configured = "not_configured"        # e.g. no search provider credentials
    parse_error = "parse_error"
    internal = "internal"


class ToolResult(BaseModel):
    """Uniform result of any external tool. Failures are explicit; a failed tool never
    yields facts other than `unavailable` ones."""

    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=64)
    ok: bool
    error: ToolError | None = None
    error_detail: str = Field(default="", max_length=300)
    evidence: list[SourceEvidence] = Field(default_factory=list, max_length=50)
    facts: list[ObservedFact] = Field(default_factory=list, max_length=200)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def _consistency(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("a successful result carries no error")
        if not self.ok:
            if self.error is None:
                raise ValueError("a failed result must say why")
            if any(f.provenance != Provenance.unavailable for f in self.facts):
                raise ValueError("a failed tool cannot report facts")
        return self


class AgentContext(BaseModel):
    """Who is calling a tool: an isolated agent identity acting for one owner.

    SECURITY: anyone can construct this object, so `capabilities` is ADVISORY only (a
    hint for display and planning). Authorization is decided exclusively by the wallet's
    PermissionChecker, which re-reads owner-issued grants from the registry. No tool or
    service may grant access because a context *says* it holds a capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # same identifier pattern as payment requests (schemas.ID_PATTERN)
    agent_id: str = Field(pattern=ID_PATTERN)
    owner_id: str = Field(pattern=ID_PATTERN)
    role: str = Field(pattern=r"^[a-z_]{3,32}$")
    capabilities: frozenset[Capability] = frozenset()


@runtime_checkable
class Tool(Protocol):
    """An external capability (search, fetch, browser...). Implementations must check
    `required_capability` against the caller and enforce their own sandbox."""

    name: str
    required_capability: Capability

    def run(self, ctx: AgentContext, **params: object) -> ToolResult: ...


@runtime_checkable
class Extractor(Protocol):
    """Turns retrieved content into provenance-tagged facts (intelligence engine side).
    The workforce calls extractors through this protocol, never by importing a concrete
    implementation, so the two sides stay decoupled. An extractor must return only
    facts it actually found in `content` (provenance=observed, source=`source`) or
    `unavailable` facts; it must never invent values."""

    name: str

    def extract(self, source: SourceEvidence, content: bytes) -> list[ObservedFact]: ...
