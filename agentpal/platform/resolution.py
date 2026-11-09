"""Resolve workspace credentials without mutating any server-wide dependency."""
from __future__ import annotations

import hashlib
import json
import threading

from .errors import PlatformError


class ProviderResolver:
    def __init__(self, integrations, server_provider, market_factory, kernel_factory):
        self.integrations, self.server_provider = integrations, server_provider
        self.market_factory, self.kernel_factory = market_factory, kernel_factory
        self._cache = {}
        self._lock = threading.RLock()

    def paypal(self, owner_id):
        fields = self.integrations.fields(owner_id, "paypal_sandbox")
        if fields is None:
            return self.server_provider
        from ..providers.config import PayPalSandboxConfig
        from ..providers.paypal_sandbox import PayPalSandboxProvider
        return self._cached(owner_id, "paypal", fields,
            lambda: PayPalSandboxProvider(PayPalSandboxConfig(**fields)))

    def _cached(self, owner_id, kind, fields, factory):
        fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).digest()
        with self._lock:
            key = owner_id, kind, fingerprint
            if key not in self._cache:
                # Bound provider objects, including obsolete keys, across a process lifetime.
                if len(self._cache) >= 1000:
                    raise PlatformError(503, "provider_capacity_reached")
                self._cache[key] = factory()
            return self._cache[key]

    def market(self, owner_id):
        fields = self.integrations.resolve(owner_id, "channel3")
        if not fields:
            return None
        return self._cached(owner_id, "channel3", fields,
            lambda: self.market_factory({"CHANNEL3_API_KEY": fields["api_key"]}))

    def kernel(self, owner_id):
        fields = self.integrations.resolve(owner_id, "kernel")
        if not fields:
            return None
        return self._cached(owner_id, "kernel", fields,
            lambda: self.kernel_factory({"KERNEL_API_KEY": fields["api_key"]}))

    def close(self):
        for (_, kind, _), value in self._cache.items():
            if kind == "paypal":
                value.http_client.close()
        self._cache.clear()
