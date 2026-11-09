"""PayPal SANDBOX implementation of ``infra_contracts.PaymentProvider`` (Specialist 4).

Sandbox only, no real money. This is not a production integration.

Official references (checked 2026-10-31):

* OAuth 2.0 client credentials: https://developer.paypal.com/api/rest/authentication/
* Orders v2: https://developer.paypal.com/docs/api/orders/v2/ and its OpenAPI document
  https://developer.paypal.com/api/orders/v2/schema.json
* Idempotency (``PayPal-Request-Id``): https://developer.paypal.com/api/rest/reference/idempotency/

Rules this module keeps:

* It never writes the ledger. It returns ``ProviderOrderRef`` / ``ProviderEvent`` and raises
  ``ProviderError``; Specialist 1's ``ProviderEventSink`` decides what they mean.
* ``PayPal-Request-Id`` is the caller-supplied key, one per (request, API operation),
  persisted by the caller BEFORE the call (SEC-20). Reusing the create key for capture is
  refused.
* No call is ever retried here. A failure after the request may have reached PayPal
  (timeout, connection reset, 5xx, unparseable success) raises
  ``ProviderError(outcome_unknown=True)``: the caller moves the payment to
  ``reconciliation_required`` and reads the order status.
* Secrets, access tokens and ``Authorization`` headers are never logged, never put in
  exception messages, and never appear in ``repr``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..infra_contracts import ProviderError, ProviderEvent, ProviderOrderRef, ProviderStatus
from ..spending import ApprovalTerms
from .config import SANDBOX_BASE_URL, PayPalSandboxConfig, assert_sandbox_base_url

logger = logging.getLogger("agentpal.providers.paypal")

PROVIDER_NAME = "paypal"
SANDBOX_PREFIX = SANDBOX_BASE_URL + "/"
TOKEN_PATH = "/v1/oauth2/token"
ORDERS_PATH = "/v2/checkout/orders"
# Refresh the cached token this many seconds before PayPal's stated expiry.
TOKEN_EXPIRY_MARGIN_S = 60
# PayPal truncates purchase_units[].description beyond 127 characters (Orders v2 schema).
DESCRIPTION_MAX = 127
EVENT_STATUS_READ = "ORDER_STATUS_READ"
EVENT_CAPTURE_RESPONSE = "ORDER_CAPTURE_RESPONSE"

_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
# Visible ASCII only, 8..128 (the ProviderOrderRef bound); safe as an HTTP header value.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7e]{8,128}$")
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.\-]")
_AMOUNT_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_REMEMBERED_CREATE_KEYS = 10_000
MAX_RESPONSE_BYTES = 1024 * 1024

DECLINE_ISSUES = frozenset({"INSTRUMENT_DECLINED", "PAYEE_ACCOUNT_INVALID",
    "PAYEE_ACCOUNT_RESTRICTED", "PAYEE_ACCOUNT_LOCKED_OR_CLOSED", "TRANSACTION_REFUSED",
    "MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED"})
BUYER_ISSUES = frozenset({"ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"})
KNOWN_ISSUES = DECLINE_ISSUES | BUYER_ISSUES | {"ORDER_ALREADY_CAPTURED", "UNPROCESSABLE_ENTITY"}


class PayPalOperationError(ProviderError):
    """Allowlisted machine outcome. The message never contains PayPal text; PayPal's
    `debug_id` (a non-secret support reference, rule 11) is kept as a separate sanitized
    attribute and logged, so operators can still trace a failed call with PayPal."""

    def __init__(self, code: str, *, outcome_unknown: bool, raw_sha256: str | None = None,
                 debug_id: str | None = None):
        self.code = code if code in KNOWN_ISSUES else "PROVIDER_ERROR"
        self.raw_sha256 = raw_sha256
        self.debug_id = debug_id or None
        super().__init__(self.code.lower(), outcome_unknown=outcome_unknown)


class PayPalOrderEvidence(BaseModel):
    """Authenticated representation, kept separate from shared provider contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    email: str | None = Field(default=None, max_length=127)
    merchant_id: str | None = Field(default=None, max_length=128)
    custom_id: str | None = Field(default=None, max_length=255)
    reference_id: str | None = Field(default=None, max_length=256)
    amount: Decimal | None = None
    currency: str | None = None
    unit_count: int


class PayPalOrderRef(ProviderOrderRef):
    order_evidence: PayPalOrderEvidence


class PayPalOrderEvent(ProviderEvent):
    order_evidence: PayPalOrderEvidence


def order_evidence(order: dict) -> PayPalOrderEvidence:
    units = order.get("purchase_units")
    unit = _first_purchase_unit(order)
    payee = unit.get("payee")
    payee = payee if isinstance(payee, dict) else {}
    amount, currency = parse_money(unit.get("amount"))
    def text_value(obj, key, limit):
        value = obj.get(key)
        return value if isinstance(value, str) and 0 < len(value) <= limit else None
    email = text_value(payee, "email_address", 127)
    return PayPalOrderEvidence(email=email.lower() if email else None,
        merchant_id=text_value(payee, "merchant_id", 128),
        custom_id=text_value(unit, "custom_id", 255),
        reference_id=text_value(unit, "reference_id", 256), amount=amount, currency=currency,
        unit_count=len(units) if isinstance(units, list) else 0)

# ---------------------------------------------------------------------------------------
# Status mapping (Orders v2 schema: order_status, capture_status)
# ---------------------------------------------------------------------------------------

ORDER_STATUS_MAP: dict[str, ProviderStatus] = {
    "CREATED": ProviderStatus.created,
    "SAVED": ProviderStatus.pending,
    "PAYER_ACTION_REQUIRED": ProviderStatus.payer_action_required,
    "APPROVED": ProviderStatus.approved_by_payer,
    "VOIDED": ProviderStatus.declined,
    # "COMPLETED" deliberately absent: the schema warns a COMPLETED order "can indicate ...
    # a payment was declined", so the outcome is taken from the capture only.
}

CAPTURE_STATUS_MAP: dict[str, ProviderStatus] = {
    "COMPLETED": ProviderStatus.completed,
    "PENDING": ProviderStatus.pending,
    "DECLINED": ProviderStatus.declined,
    "FAILED": ProviderStatus.declined,
    "REFUNDED": ProviderStatus.refunded,
    # Lead decision: a partial refund maps to refunded; the event carries the REFUNDED
    # amount, so S1's sink (amount must equal the approved terms) refuses it and the
    # payment stays counted as spent. Fails safe for the budget.
    "PARTIALLY_REFUNDED": ProviderStatus.refunded,
}


def map_order_status(order_status: str | None,
                     capture_status: str | None = None) -> ProviderStatus:
    """Map an Orders v2 order status (and, when present, the single capture's status)."""
    if capture_status is not None:
        return CAPTURE_STATUS_MAP.get(capture_status, ProviderStatus.unknown)
    if order_status == "COMPLETED":
        return ProviderStatus.unknown           # completed order without a capture: unknown
    return ORDER_STATUS_MAP.get(order_status or "", ProviderStatus.unknown)


def _safe(text: Any, limit: int = 64) -> str:
    return _SAFE_TOKEN_RE.sub("", str(text))[:limit] if text is not None else ""


def parse_money(obj: Any) -> tuple[Decimal | None, str | None]:
    """``{"currency_code": "USD", "value": "1.00"}`` -> (Decimal, "USD"); invalid -> None."""
    if not isinstance(obj, dict):
        return None, None
    value, currency = obj.get("value"), obj.get("currency_code")
    if not isinstance(value, str) or len(value) > 32 or not _AMOUNT_RE.fullmatch(value):
        return None, None
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        return None, None
    try:
        return Decimal(value), currency
    except InvalidOperation:
        return None, None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def format_amount(amount: Decimal) -> str:
    """Decimal string with no exponent (``ApprovalTerms`` already quantized it)."""
    return format(amount, "f")


def paypal_request_id(operation: str, idempotency_key: str) -> str:
    """The `PayPal-Request-Id` actually sent: derived from (operation, caller key).

    A create header and a capture header can therefore never be equal, on any instance or
    after any restart (SEC-20), while a retry with the same caller key and operation sends
    the same header (idempotent). Lead fix after the Codex review, 2026-10-31."""
    if operation not in ("create", "capture"):
        raise ValueError("unknown idempotent operation")
    digest = hashlib.sha256(f"{operation}|{idempotency_key}".encode()).hexdigest()[:40]
    return f"agentpal-{operation}-{digest}"


def validate_idempotency_key(key: Any) -> str:
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY_RE.match(key):
        raise ProviderError("idempotency key must be 8-128 visible ASCII characters",
                            outcome_unknown=False)
    return key


def validate_order_id(order_id: Any) -> str:
    if not isinstance(order_id, str) or not _ORDER_ID_RE.match(order_id):
        raise ProviderError("invalid provider order id", outcome_unknown=False)
    return order_id


def _clean_text(text: str) -> str:
    return _CONTROL_RE.sub(" ", text).strip()


def _first_purchase_unit(order: dict) -> dict:
    units = order.get("purchase_units")
    if isinstance(units, list) and units and isinstance(units[0], dict):
        return units[0]
    return {}


def _payments(order: dict) -> dict:
    p = _first_purchase_unit(order).get("payments")
    return p if isinstance(p, dict) else {}


def _completed_refund_total(payments: dict) -> tuple[Decimal | None, str | None]:
    refunds = payments.get("refunds")
    if not isinstance(refunds, list):
        return None, None
    total, currency = Decimal(0), None
    for r in refunds:
        if not isinstance(r, dict) or r.get("status") != "COMPLETED":
            continue
        amt, cur = parse_money(r.get("amount"))
        if amt is None or (currency is not None and cur != currency):
            return None, None
        total += abs(amt)
        currency = cur
    return (total, currency) if currency is not None else (None, None)


class PayPalSandboxProvider:
    """``PaymentProvider`` for the PayPal sandbox. Thread-safe token cache."""

    name: str = PROVIDER_NAME
    environment: Literal["sandbox"] = "sandbox"

    def __init__(self, config: PayPalSandboxConfig, *, http_client: httpx.Client | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(config, PayPalSandboxConfig):
            raise TypeError("config must be a PayPalSandboxConfig")
        self._config = config
        self._base_url = assert_sandbox_base_url(config.base_url)
        self._timeout = httpx.Timeout(config.read_timeout_s, connect=config.connect_timeout_s)
        self._http = http_client or httpx.Client(timeout=self._timeout, follow_redirects=False)
        self._monotonic = monotonic
        self._now = now
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._create_keys: OrderedDict[str, None] = OrderedDict()
        self._webhooks = None   # built lazily (keeps the cryptography import optional)

    def __repr__(self) -> str:
        return f"PayPalSandboxProvider(environment='sandbox', base_url='{self._base_url}')"

    __str__ = __repr__

    @property
    def config(self) -> PayPalSandboxConfig:
        return self._config

    @property
    def http_client(self) -> httpx.Client:
        return self._http

    @property
    def timeout(self) -> httpx.Timeout:
        return self._timeout

    def _bounded_request(self, method, url, *, headers, content, mutating):
        with self._http.stream(method, url, headers=headers, content=content,
                               timeout=self._timeout, follow_redirects=False) as response:
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=16384):
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ProviderError("provider_response_too_large", outcome_unknown=mutating)
                raw.extend(chunk)
            return httpx.Response(response.status_code, content=bytes(raw))

    # ------------------------------------------------------------------ OAuth
    def access_token(self) -> str:
        """Client-credentials token, cached until shortly before ``expires_in`` runs out.
        Fetching a token moves no money, so every failure is ``outcome_unknown=False``."""
        with self._lock:
            if self._token is not None and self._monotonic() < self._token_expires_at:
                return self._token
            self._token = None
            secret = self._config.client_secret.get_secret_value()
            basic = base64.b64encode(f"{self._config.client_id}:{secret}".encode()).decode()
            try:
                resp = self._bounded_request(
                    "POST", self._base_url + TOKEN_PATH,
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    content=b"grant_type=client_credentials", mutating=False)
            except httpx.HTTPError as exc:
                raise ProviderError(f"paypal oauth request failed: {type(exc).__name__}",
                                    outcome_unknown=False) from None
            if resp.status_code != 200:
                raise ProviderError(f"paypal oauth failed: http {resp.status_code}",
                                    outcome_unknown=False)
            try:
                data = resp.json()
                token = data["access_token"]
                expires_in = int(data["expires_in"])
            except (ValueError, KeyError, TypeError):
                raise ProviderError("paypal oauth response malformed",
                                    outcome_unknown=False) from None
            if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                    or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)
                    or not 1 <= expires_in <= 86400):
                raise ProviderError("paypal oauth response malformed", outcome_unknown=False)
            if str(data.get("token_type", "Bearer")).lower() != "bearer":
                raise ProviderError("paypal oauth token type unsupported",
                                    outcome_unknown=False)
            self._token = token
            self._token_expires_at = self._monotonic() + max(
                0, expires_in - TOKEN_EXPIRY_MARGIN_S)
            logger.info("paypal sandbox access token obtained (expires_in=%ss)", expires_in)
            return token

    def invalidate_token(self) -> None:
        with self._lock:
            self._token = None
            self._token_expires_at = 0.0

    # ------------------------------------------------------------------ HTTP core
    def _call(self, method: str, path: str, *, operation: str, mutating: bool,
              body: bytes | None = None, request_id: str | None = None,
              mock_response: str | None = None
              ) -> tuple[dict, bytes]:
        url = self._base_url + path
        if not url.startswith(SANDBOX_PREFIX):        # defence in depth: sandbox host only
            raise ProviderError("refusing non-sandbox URL", outcome_unknown=False)
        if mock_response is not None:
            if not self._config.negative_testing:
                raise ProviderError("negative_testing_disabled", outcome_unknown=False)
            if (self._base_url != SANDBOX_BASE_URL or mock_response not in KNOWN_ISSUES
                    or operation not in ("create_order", "capture_order")):
                raise ProviderError("negative_testing_refused", outcome_unknown=False)
        token = self.access_token()                    # pre-send: failures are not unknown
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Prefer": "return=representation"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if request_id is not None:
            headers["PayPal-Request-Id"] = request_id
        if mock_response is not None:
            headers["PayPal-Mock-Response"] = json.dumps({"mock_application_codes": mock_response})
        try:
            resp = self._bounded_request(method, url, headers=headers, content=body,
                                         mutating=mutating)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The connection was never established: PayPal cannot have processed anything.
            raise ProviderError(f"paypal {operation}: not sent ({type(exc).__name__})",
                                outcome_unknown=False) from None
        except httpx.HTTPError as exc:
            # Read/write timeout, reset, protocol error AFTER sending: outcome unknown.
            logger.warning("paypal %s outcome unknown (%s)", operation, type(exc).__name__)
            raise ProviderError(f"paypal {operation}: outcome unknown ({type(exc).__name__})",
                                outcome_unknown=True) from None
        return self._interpret(resp, operation=operation, mutating=mutating)

    def _interpret(self, resp: httpx.Response, *, operation: str, mutating: bool
                   ) -> tuple[dict, bytes]:
        status, raw = resp.status_code, resp.content
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider_response_too_large", outcome_unknown=mutating)
        if 200 <= status < 300:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise ProviderError(f"paypal {operation}: unparseable success response",
                                    outcome_unknown=True)
            return data, raw
        issues, debug_id = [], ""
        try:
            err = json.loads(raw)
            if isinstance(err, dict):
                debug_id = _safe(err.get("debug_id"))
                details = err.get("details")
                if isinstance(details, list) and len(details) <= 32:
                    issues = [d.get("issue") for d in details if isinstance(d, dict)]
        except ValueError:
            pass
        # Mixed or unrecognized details never authorize release. A bare generic
        # UNPROCESSABLE_ENTITY is insufficient evidence of a definitive decline.
        code = issues[0] if len(issues) == 1 and isinstance(issues[0], str) and issues[0] in KNOWN_ISSUES else "PROVIDER_ERROR"
        definite = 400 <= status < 500 and status != 409 and code in DECLINE_ISSUES | BUYER_ISSUES
        unknown = mutating and not definite
        if status == 401:
            self.invalidate_token()
            unknown = False
        elif status >= 500 or status < 400 or status == 409:
            unknown = mutating
            code = "PROVIDER_ERROR"
        logger.warning("paypal %s http %s code=%s debug_id=%s (outcome_unknown=%s)",
                       operation, status, code, debug_id or "-", unknown)
        raise PayPalOperationError(code, outcome_unknown=unknown,
                                   raw_sha256=hashlib.sha256(raw).hexdigest(), debug_id=debug_id)

    # ------------------------------------------------------------------ Orders v2
    def remember_create_key(self, idempotency_key: str) -> None:
        with self._lock:
            self._create_keys[idempotency_key] = None
            self._create_keys.move_to_end(idempotency_key)
            while len(self._create_keys) > _MAX_REMEMBERED_CREATE_KEYS:
                self._create_keys.popitem(last=False)

    def build_order_body(self, terms: ApprovalTerms, return_url: str | None = None) -> dict:
        if terms.amount <= 0:
            raise ProviderError("amount must be positive", outcome_unknown=False)
        description = _clean_text(f"{terms.merchant_name}: {terms.purpose}")[:DESCRIPTION_MAX]
        unit: dict[str, Any] = {
            "reference_id": terms.request_id[:256],
            "custom_id": terms.request_id[:255],
            "amount": {"currency_code": terms.currency, "value": format_amount(terms.amount)},
        }
        if terms.merchant_paypal_email is not None:
            unit["payee"] = {"email_address": terms.merchant_paypal_email}
        if description:
            unit["description"] = description
        body: dict[str, Any] = {"intent": "CAPTURE", "purchase_units": [unit]}
        if return_url is not None:
            parts = urlsplit(return_url)
            if parts.scheme not in ("https", "http") or not parts.netloc or len(return_url) > 2048:
                raise ProviderError("invalid return_url", outcome_unknown=False)
            body["payment_source"] = {"paypal": {"experience_context": {"return_url": return_url}}}
        return body

    def create_order(self, terms: ApprovalTerms, *, idempotency_key: str,
                     return_url: str | None = None,
                     mock_response: str | None = None) -> ProviderOrderRef:
        validate_idempotency_key(idempotency_key)
        body = self.build_order_body(terms, return_url)
        # Remembered before the call: even an unknown outcome makes this a create key.
        self.remember_create_key(idempotency_key)
        data, _raw = self._call("POST", ORDERS_PATH, operation="create_order", mutating=True,
                                body=json.dumps(body, separators=(",", ":")).encode(),
                                request_id=paypal_request_id("create", idempotency_key),
                                mock_response=mock_response)
        order_id = data.get("id")
        if not isinstance(order_id, str) or not _ORDER_ID_RE.match(order_id):
            raise ProviderError("paypal create_order: response without a valid order id",
                                outcome_unknown=True)
        status = map_order_status(data.get("status") if isinstance(data.get("status"), str)
                                  else None)
        ref = PayPalOrderRef(order_evidence=order_evidence(data),
                               provider=PROVIDER_NAME, environment="sandbox",
                               provider_order_id=order_id, idempotency_key=idempotency_key,
                               status=status, payer_action_url=self._payer_link(data))
        logger.info("paypal create_order ok order_id=%s status=%s", order_id, status.value)
        return ref

    @staticmethod
    def _payer_link(order: dict) -> str | None:
        links = order.get("links")
        if not isinstance(links, list):
            return None
        # E2E-H3-2: upstream JSON is untrusted; a non-string rel (e.g. a list) must be skipped,
        # not crash the request with an unhashable-key error.
        by_rel = {link.get("rel"): link.get("href") for link in links
                  if isinstance(link, dict) and isinstance(link.get("rel"), str)}
        for rel in ("payer-action", "approve"):
            href = by_rel.get(rel)
            if (not isinstance(href, str) or len(href) > 2048
                    or any(ord(c) < 33 for c in href)):
                continue
            try:
                parts = urlsplit(href)
                host = (parts.hostname or "").lower()
                safe_origin = (parts.port in (None, 443) and parts.username is None
                               and parts.password is None)
            except ValueError:
                continue
            if (safe_origin and parts.scheme == "https"
                    and (host == "sandbox.paypal.com" or host.endswith(".sandbox.paypal.com"))):
                return href
            logger.warning("paypal payer link with a non-sandbox host dropped")
        return None

    def capture_order(self, provider_order_id: str, *, idempotency_key: str,
                      mock_response: str | None = None) -> ProviderEvent:
        validate_order_id(provider_order_id)
        validate_idempotency_key(idempotency_key)
        with self._lock:
            reused = idempotency_key in self._create_keys
        if reused:
            # SEC-20: one PayPal-Request-Id per (request, operation); never the create key.
            raise ProviderError("capture idempotency key equals a create key (SEC-20)",
                                outcome_unknown=False)
        data, raw = self._call("POST", f"{ORDERS_PATH}/{provider_order_id}/capture",
                               operation="capture_order", mutating=True, body=b"{}",
                               request_id=paypal_request_id("capture", idempotency_key),
                               mock_response=mock_response)
        event = self.order_event(data, raw, expected_order_id=provider_order_id,
                                 event_type=EVENT_CAPTURE_RESPONSE)
        logger.info("paypal capture_order order_id=%s status=%s", provider_order_id,
                    event.status.value)
        return event

    def get_order_status(self, provider_order_id: str) -> ProviderEvent:
        validate_order_id(provider_order_id)
        data, raw = self._call("GET", f"{ORDERS_PATH}/{provider_order_id}",
                               operation="get_order_status", mutating=False)
        return self.order_event(data, raw, expected_order_id=provider_order_id,
                                event_type=EVENT_STATUS_READ)

    def order_event(self, order: dict, raw: bytes, *, expected_order_id: str,
                    event_type: str) -> ProviderEvent:
        """An authenticated server-to-server order representation -> verified event with a
        deterministic event id (same observed state -> same id, so the sink dedups)."""
        if order.get("id") != expected_order_id:
            raise ProviderError("paypal response is for a different order",
                                outcome_unknown=True)
        order_status = order.get("status") if isinstance(order.get("status"), str) else None
        payments = _payments(order)
        captures = payments.get("captures")
        malformed_captures = captures is not None and (
            not isinstance(captures, list) or len(captures) > 1
            or any(not isinstance(c, dict) for c in captures))
        captures = [c for c in captures if isinstance(c, dict)] if isinstance(captures, list) else []
        capture = captures[0] if len(captures) == 1 else None
        capture_id = capture.get("id") if capture and isinstance(capture.get("id"), str) else None
        capture_status = (capture.get("status") if capture and isinstance(capture.get("status"), str)
                          else None)
        if (malformed_captures or len(captures) > 1
                or (capture is not None and (
                    not isinstance(capture_id, str) or not _ORDER_ID_RE.fullmatch(capture_id)))):
            status = ProviderStatus.unknown         # single-capture orders only
        else:
            status = map_order_status(order_status, capture_status)
        if status is ProviderStatus.refunded:
            amount, currency = _completed_refund_total(payments)   # the REFUNDED amount
        elif capture is not None:
            amount, currency = parse_money(capture.get("amount"))
        else:
            amount, currency = parse_money(_first_purchase_unit(order).get("amount"))
        occurred = None
        if capture is not None:
            occurred = parse_time(capture.get("update_time")) or parse_time(
                capture.get("create_time"))
        occurred = occurred or parse_time(order.get("update_time")) or parse_time(
            order.get("create_time"))
        evidence = order_evidence(order)
        fingerprint = "|".join([evidence.model_dump_json(), expected_order_id, order_status or "", capture_id or "",
                                capture_status or "", str(amount) if amount is not None else "",
                                currency or ""])
        event_id = "q_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:40]
        return PayPalOrderEvent(order_evidence=evidence,
            provider=PROVIDER_NAME, environment="sandbox", event_id=event_id,
            event_type=event_type, provider_order_id=expected_order_id,
            provider_capture_id=capture_id[:128] if capture_id else None, status=status,
            amount=amount, currency=currency, verified=True, occurred_at=occurred,
            received_at=self._now(), raw_sha256=hashlib.sha256(raw).hexdigest())

    # ------------------------------------------------------------------ webhooks
    def _webhook_verifier(self):
        if self._webhooks is None:
            from .paypal_webhooks import PayPalWebhookVerifier
            self._webhooks = PayPalWebhookVerifier(
                self._config, http_client=self._http, token_provider=self.access_token,
                now=self._now, timeout=self._timeout)
        return self._webhooks

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> ProviderEvent:
        """Verify a webhook delivery and parse it. Raises ``WebhookVerificationError`` on
        any failure; returns a ``verified=True`` event only after verification passed."""
        return self._webhook_verifier().verify(headers, raw_body)

