"""Web search tool (`research.search`) behind a provider interface.

No search API credentials exist today, so the default provider is
`NotConfiguredSearchProvider`, which makes the tool return `ToolError.not_configured` with
what is needed. To enable search, an operator must choose a provider (e.g. Brave Search
API, Bing Web Search API, SerpAPI, Google Programmable Search), store its API key as a
secret (never in the repository), implement `SearchProvider` for it, and inject it.

Search results are LEADS, not facts: a hit's title and snippet are what the search engine
claims about a page. The tool records each hit as `SourceEvidence(method="search_result")`
(hash of the hit as returned) and emits no facts; the research agent must fetch the
original source before anything is extracted from it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import AgentContext, Capability, SourceEvidence, ToolError, ToolResult
from .base import PermissionAuthority, failed, gate, utc_now
from .evidence_store import EvidenceStore

MAX_QUERY_LENGTH = 300
MAX_RESULTS = 10


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str = Field(min_length=8, max_length=2048)
    title: str = Field(default="", max_length=300)
    snippet: str = Field(default="", max_length=1000)

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")


class SearchNotConfigured(RuntimeError):
    pass


class SearchProvider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def search(self, query: str, max_results: int) -> Sequence[SearchHit]: ...


class NotConfiguredSearchProvider:
    name = "not_configured"
    requirements = ("no web search provider is configured: choose one (e.g. Brave Search "
                    "API, Bing Web Search API, SerpAPI, Google Programmable Search), store "
                    "its API key as a secret, and inject a SearchProvider for it")

    def configured(self) -> bool:
        return False

    def search(self, query: str, max_results: int) -> Sequence[SearchHit]:
        raise SearchNotConfigured(self.requirements)


class WebSearchTool:
    name = "web_search"
    required_capability = Capability.research_search

    def __init__(self, checker: PermissionAuthority,
                 provider: SearchProvider | None = None,
                 evidence_store: EvidenceStore | None = None,
                 clock: Callable[[], datetime] = utc_now):
        self.checker = checker
        self.provider = provider or NotConfiguredSearchProvider()
        self.evidence_store = evidence_store
        self.clock = clock

    def run(self, ctx: AgentContext, **params: object) -> ToolResult:
        started = self.clock()
        denied = gate(self.name, self.required_capability, ctx, self.checker, started,
                      self.clock)
        if denied:
            return denied
        query = params.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            return failed(self.name, ToolError.parse_error,
                          f"query must be 1-{MAX_QUERY_LENGTH} characters", started,
                          self.clock)
        max_results = params.get("max_results", 5)
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            max_results = 5
        max_results = max(1, min(MAX_RESULTS, max_results))
        if not self.provider.configured():
            return failed(self.name, ToolError.not_configured,
                          getattr(self.provider, "requirements", "search not configured"),
                          started, self.clock)
        try:
            hits = list(self.provider.search(query.strip(), max_results))[:max_results]
        except SearchNotConfigured as exc:
            return failed(self.name, ToolError.not_configured, str(exc), started, self.clock)
        except Exception as exc:  # provider failure never becomes a result
            return failed(self.name, ToolError.http_error,
                          f"search provider failed ({type(exc).__name__})", started,
                          self.clock)
        evidence = []
        now = self.clock()
        for hit in hits:
            if not isinstance(hit, SearchHit) or not hit.url.startswith("https://"):
                continue  # only https leads; anything else is dropped
            raw = hit.canonical_bytes()
            sha = (self.evidence_store.put(raw) if self.evidence_store is not None
                   else SourceEvidence.digest(raw))
            evidence.append(SourceEvidence(url=hit.url, retrieved_at=now, content_sha256=sha,
                                           method="search_result"))
        return ToolResult(tool=self.name, ok=True, evidence=evidence, started_at=started,
                          finished_at=self.clock())
