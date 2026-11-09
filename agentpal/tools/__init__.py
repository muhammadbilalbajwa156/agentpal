"""External tools for the workforce (`contracts.Tool` implementations).

* `HttpFetchTool`: sandboxed HTTP(S) fetch with SSRF guard, IP pinning, limits, evidence;
* `WebSearchTool`: search behind a provider interface (not configured today);
* `EvidenceStore`: content-addressed, write-once store of retrieved bytes.

Every tool requires an injected permission authority and fails closed without one. No tool
touches the wallet, the ledger or a payment provider. See docs/WORKFORCE_ARCHITECTURE.md.
"""

from .evidence_store import DEFAULT_EVIDENCE_ROOT, EvidenceError, EvidenceStore
from .fetch import HttpFetchTool
from .netguard import BlockedTarget, VettedTarget, ip_is_public, system_resolver, vet_url
from .search import NotConfiguredSearchProvider, SearchHit, SearchProvider, WebSearchTool

__all__ = [
    "DEFAULT_EVIDENCE_ROOT", "BlockedTarget", "EvidenceError", "EvidenceStore",
    "HttpFetchTool", "NotConfiguredSearchProvider", "SearchHit", "SearchProvider",
    "VettedTarget", "WebSearchTool", "ip_is_public", "system_resolver", "vet_url",
]
