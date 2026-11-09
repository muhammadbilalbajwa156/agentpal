"""Guarded helpers shared by the example external agents (Shopper, Company operators).

Everything here is deterministic and offline-testable. The helpers never fetch product or
buyer URLs, never surface remote error bodies, and redact configured secrets.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import quote, urlsplit

import httpx

FREE_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "google/gemma-4-31b-it:free",
]
STOP = {"provider_confirmed", "rejected", "expired", "failed", "reversed",
        "reconciliation_required"}
STATUSES = STOP | {"prepared", "awaiting_approval", "approved", "submitted_to_provider",
                   "provider_pending"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MONEY = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?\Z")
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Instruction-shaped catalogue text is withheld from the model (it stays untrusted data).
UNTRUSTED_TEXT = re.compile(r"ignore|instruction|system|assistant|approve|capture|refund|"
                            r"secret|credential|tool[_ ]?call", re.I)
FREE_MODEL = re.compile(r"[a-zA-Z0-9._/-]+:free")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AgentError(RuntimeError):
    """Stable, non-reflective error codes; never contains remote bodies or secrets."""


class RemoteRefused(AgentError):
    """A definitive non-2xx answer (no body is kept). ``status`` is the HTTP status."""

    def __init__(self, status: int):
        super().__init__("remote_request_refused")
        self.status = status


def money(value: object, ceiling: Decimal | None = None) -> str:
    if not isinstance(value, str) or not MONEY.fullmatch(value):
        raise AgentError("invalid_decimal_amount")
    amount = Decimal(value)
    if amount <= 0 or (ceiling is not None and amount > ceiling):
        raise AgentError("budget_guard_refused")
    return format(amount, ".2f")


def text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        raise AgentError("invalid_text")
    value = re.sub(r"<[^>]*>", " ", value)
    value = " ".join("".join(c for c in value if c.isprintable()).split())[:limit]
    if not value:
        raise AgentError("empty_text")
    return value


def untrusted_text(value: object, limit: int, placeholder: str) -> str:
    """Bounded display text for the model; instruction-shaped text is replaced."""
    value = text(value, limit)
    return placeholder if UNTRUSTED_TEXT.search(value) or EMAIL.search(value) else value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise AgentError("invalid_identifier")
    return value


def display_url(value: object, buyer: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise AgentError("invalid_display_url")
    try:
        p = urlsplit(value)
        valid = (p.scheme == "https" and p.hostname and not p.username and not p.password
                 and p.port in (None, 443) and not any(c.isspace() for c in value))
        if buyer:
            valid = valid and p.hostname == "www.sandbox.paypal.com"
        if not valid:
            raise ValueError
    except ValueError:
        raise AgentError("invalid_display_url") from None
    return value


def base_url(value: str) -> str:
    """HTTPS origin, or plain HTTP on loopback only; no path, credentials or query."""
    value = value.rstrip("/")
    try:
        p = urlsplit(value)
        if (not p.hostname or p.username or p.password or p.query or p.fragment
                or p.path or (p.scheme != "https" and not
                             (p.scheme == "http" and p.hostname in
                              {"127.0.0.1", "localhost", "::1"}))):
            raise ValueError
        _ = p.port  # Validate a malformed or out-of-range port.
    except ValueError:
        raise AgentError("invalid_agentpal_base_url") from None
    return value


GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
GROQ_MODEL = re.compile(r"[a-zA-Z0-9._/-]{1,100}")


def groq_models(raw_models: str) -> list[str]:
    """Parse GROQ_MODELS (CSV). Defaults to open-weight models on Groq's free tier."""
    models = [m.strip() for m in raw_models.split(",") if m.strip()] if raw_models else GROQ_MODELS
    if not 1 <= len(models) <= 5 or any(not GROQ_MODEL.fullmatch(m) for m in models):
        raise AgentError("invalid_groq_models")
    return models


def free_models(raw_models: str) -> list[str]:
    """Parse OPENROUTER_MODELS (CSV or JSON array). Every model must be a ``:free`` model."""
    try:
        models = json.loads(raw_models) if raw_models.lstrip().startswith("[") else (
            raw_models.split(",") if raw_models else FREE_MODELS)
        if (not isinstance(models, list) or not 1 <= len(models) <= 10
                or any(not isinstance(m, str) or not FREE_MODEL.fullmatch(m.strip())
                       for m in models)):
            raise ValueError
    except (ValueError, TypeError):
        raise AgentError("free_models_required") from None
    return [m.strip() for m in models]


class Redactor:
    def __init__(self, env: dict[str, str], extra: list[str] | tuple[str, ...] = ()):
        values = [v for k, v in env.items()
                  if v and re.search(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)$", k.upper())]
        values.extend(v for v in extra if v)
        self._values = sorted({x for v in values for x in (v, quote(v, safe=""))},
                              key=len, reverse=True)

    def __call__(self, value: str) -> str:
        for secret in self._values:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"\b(?:apk_|apo_|sk-)[A-Za-z0-9_-]+", "[REDACTED]", value)

    def clean(self, value):
        # Walk strings rather than replacing serialized JSON (keys can contain quotes).
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {self(str(k)): self.clean(v) for k, v in value.items()}
        return value


def bounded_json(client: httpx.Client, method: str, url: str, headers: dict, body,
                 redact: Redactor) -> dict:
    """One bounded request: no redirects, 2 MiB cap, JSON object only, redacted result.

    Callers decide the allow-listed destination; this function never builds a URL."""
    try:
        with client.stream(method, url, headers=headers, json=body,
                           follow_redirects=False, timeout=20) as response:
            if response.status_code == 429:
                raise AgentError("rate_limited")
            if not 200 <= response.status_code < 300:
                raise RemoteRefused(response.status_code)
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise AgentError("remote_response_too_large")
                chunks.append(chunk)
            result = json.loads(b"".join(chunks))
            if not isinstance(result, dict):
                raise ValueError
            return redact.clean(result)
    except (httpx.HTTPError, ValueError, TypeError):
        raise AgentError("remote_unavailable_or_invalid") from None
