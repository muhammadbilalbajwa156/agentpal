"""PayPal SANDBOX webhook verification and parsing (Specialist 4).

Official references (checked 2026-10-31):

* Webhooks overview, self-verification and postback:
  https://developer.paypal.com/api/rest/webhooks/rest/
* Webhooks v1 API (``/v1/notifications/verify-webhook-signature``, ``PAYPAL-AUTH-ALGO``):
  https://developer.paypal.com/api/webhooks/v1/schema.json
* Event names: https://developer.paypal.com/api/rest/webhooks/event-names/

Primary method: SELF-VERIFICATION. PayPal signs
``transmissionId|timeStamp|webhookId|crc32`` (CRC32 in decimal over the exact raw body)
with SHA256withRSA; the public key is in the certificate at ``paypal-cert-url``.
``webhookId`` is OUR configured ``PAYPAL_SANDBOX_WEBHOOK_ID``, never a value from the
request. Optional mode ``postback`` asks PayPal's ``verify-webhook-signature`` endpoint
instead, sending the raw event bytes unchanged.

AgentPal policies on top of PayPal's documentation (not PayPal rules):

* the cert URL must be https on a PayPal SANDBOX API host, path
  ``/v1/notifications/certs/<id>``; nothing else is fetched;
* the transmission time must be within ``webhook_max_age_s`` (default 300 s) of now;
* the signing certificate must chain to certifi trust anchors and match the configured
  signing SAN. Bundled/operator intermediates are untrusted; AIA is never fetched.

Simulator mock events carry the webhook id ``WEBHOOK_ID`` and are not supported by the
postback API; since the configured id can never be ``WEBHOOK_ID``, they fail both modes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import threading
import zlib
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..infra_contracts import ProviderEvent, ProviderStatus
from .config import SANDBOX_BASE_URL, PayPalSandboxConfig
from .paypal_sandbox import (
    CAPTURE_STATUS_MAP,
    PROVIDER_NAME,
    map_order_status,
    parse_money,
    parse_time,
)

try:  # optional at import time; self-verification refuses to run without it
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - exercised by monkeypatching the flag
    HAVE_CRYPTOGRAPHY = False

logger = logging.getLogger("agentpal.providers.paypal.webhooks")

H_TRANSMISSION_ID = "paypal-transmission-id"
H_TRANSMISSION_TIME = "paypal-transmission-time"
H_TRANSMISSION_SIG = "paypal-transmission-sig"
H_CERT_URL = "paypal-cert-url"
H_AUTH_ALGO = "paypal-auth-algo"
REQUIRED_HEADERS = (H_TRANSMISSION_ID, H_TRANSMISSION_TIME, H_TRANSMISSION_SIG, H_CERT_URL,
                    H_AUTH_ALGO)
SUPPORTED_AUTH_ALGO = "SHA256withRSA"

# Sandbox API hosts only; production hosts are refused (sandbox-only adapter).
ALLOWED_CERT_HOSTS = frozenset({"api.sandbox.paypal.com", "api-m.sandbox.paypal.com"})
_CERT_PATH_RE = re.compile(r"^/v1/notifications/certs/[A-Za-z0-9-]{1,200}$")
MAX_CERT_BYTES = 64 * 1024
MAX_VERIFY_RESPONSE_BYTES = 64 * 1024   # PayPal's verify response is a tiny JSON object
MAX_BODY_BYTES = 512 * 1024
_MAX_CACHED_CERTS = 32
VERIFY_PATH = "/v1/notifications/verify-webhook-signature"

# event_type -> how to read it. "order": resource is an order; "capture": resource is a
# capture; "refund": resource is a refund/reversal (amount = refunded amount);
# "approval_reversed": resource names the order.
EVENT_KINDS: dict[str, tuple[str, ProviderStatus | None]] = {
    "CHECKOUT.ORDER.APPROVED": ("order", ProviderStatus.approved_by_payer),
    "CHECKOUT.ORDER.COMPLETED": ("order", None),          # status from the capture
    "CHECKOUT.PAYMENT-APPROVAL.REVERSED": ("approval_reversed", ProviderStatus.declined),
    "PAYMENT.CAPTURE.COMPLETED": ("capture", ProviderStatus.completed),
    "PAYMENT.CAPTURE.PENDING": ("capture", ProviderStatus.pending),
    "PAYMENT.CAPTURE.DECLINED": ("capture", ProviderStatus.declined),   # v2 table name
    "PAYMENT.CAPTURE.DENIED": ("capture", ProviderStatus.declined),     # v1 table name
    "PAYMENT.CAPTURE.REFUNDED": ("refund", ProviderStatus.refunded),
    "PAYMENT.CAPTURE.REVERSED": ("refund", ProviderStatus.reversed),
}


class WebhookVerificationError(ValueError):
    """The delivery is not a verified PayPal sandbox event. The message is generic and
    never contains header values or the body."""


class WebhookConfigError(WebhookVerificationError):
    """Verification cannot run (no webhook id configured, cryptography missing)."""


class UnsupportedEventType(WebhookVerificationError):
    """The body names an event type AgentPal does not consume. On its own this is a
    rejection: it says nothing about who sent the body."""


class VerifiedEventIgnored(Exception):
    """Raised ONLY by ``PayPalWebhookVerifier.verify`` after the signature verified: a
    genuine PayPal event whose type AgentPal does not consume (for example payout
    events). Acknowledge it (2xx, nothing applied) so PayPal stops retrying. Deliberately
    not a ``WebhookVerificationError``."""

    def __init__(self, event_type: str):
        super().__init__("verified event type not consumed")
        self.event_type = event_type


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in dict(headers).items():
        if isinstance(k, str) and isinstance(v, str):
            out[k.strip().lower()] = v.strip()
    return out


def check_cert_url(url: str) -> str:
    """Pin the certificate URL to PayPal sandbox API hosts before anything is fetched."""
    if not isinstance(url, str) or len(url) > 500:
        raise WebhookVerificationError("cert url rejected")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WebhookVerificationError("cert url rejected") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or (port not in (None, 443)) or parts.query or parts.fragment
            or (parts.hostname or "") not in ALLOWED_CERT_HOSTS
            or parts.netloc.lower().split(":")[0] not in ALLOWED_CERT_HOSTS
            or not _CERT_PATH_RE.match(parts.path)):
        raise WebhookVerificationError("cert url rejected")
    return url


def crc32_decimal(raw_body: bytes) -> int:
    return zlib.crc32(raw_body) & 0xFFFFFFFF


def signed_message(transmission_id: str, transmission_time: str, webhook_id: str,
                   raw_body: bytes) -> bytes:
    return (f"{transmission_id}|{transmission_time}|{webhook_id}|"
            f"{crc32_decimal(raw_body)}").encode()


class PayPalWebhookVerifier:
    def __init__(self, config: PayPalSandboxConfig, *, http_client: httpx.Client,
                 token_provider: Callable[[], str], now: Callable[[], datetime],
                 timeout: httpx.Timeout | None = None):
        self._config = config
        self._http = http_client
        self._token = token_provider
        self._now = now
        self._timeout = timeout or httpx.Timeout(10.0, connect=5.0)
        self._lock = threading.Lock()
        self._certs: dict[str, Any] = {}
        self._trust_store = None  # tests may inject a private test PKI; runtime uses certifi

    # ---------------------------------------------------------------- entry point
    def verify(self, headers: dict[str, str], raw_body: bytes) -> ProviderEvent:
        webhook_id = self._config.webhook_id
        if not webhook_id:
            raise WebhookConfigError("PAYPAL_SANDBOX_WEBHOOK_ID is not configured")
        if not isinstance(raw_body, (bytes, bytearray)) or not raw_body:
            raise WebhookVerificationError("empty body")
        raw_body = bytes(raw_body)
        if len(raw_body) > MAX_BODY_BYTES:
            raise WebhookVerificationError("body too large")
        h = normalize_headers(headers)
        if any(not h.get(name) for name in REQUIRED_HEADERS):
            raise WebhookVerificationError("missing PayPal transmission headers")
        if h[H_AUTH_ALGO] != SUPPORTED_AUTH_ALGO:
            raise WebhookVerificationError("unsupported auth algorithm")
        self._check_fresh(h[H_TRANSMISSION_TIME])
        check_cert_url(h[H_CERT_URL])
        if self._config.webhook_verification == "postback":
            self._postback(h, raw_body, webhook_id)
        else:
            self._self_verify(h, raw_body, webhook_id)
        try:
            event = parse_webhook_event(raw_body, received_at=self._now(),
                                        capture_order_lookup=self._capture_order_id)
        except UnsupportedEventType:
            event_type = json.loads(raw_body).get("event_type")
            safe = (event_type if isinstance(event_type, str)
                    and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", event_type) else "unrecognized")
            raise VerifiedEventIgnored(safe) from None
        logger.info("paypal webhook verified event_type=%s order_id=%s status=%s",
                    event.event_type, event.provider_order_id, event.status.value)
        return event

    def _check_fresh(self, transmission_time: str) -> None:
        sent = parse_time(transmission_time)
        if sent is None:
            raise WebhookVerificationError("bad transmission time")
        window = timedelta(seconds=self._config.webhook_max_age_s)
        if abs(self._now() - sent) > window:
            raise WebhookVerificationError("transmission time outside the freshness window")

    # ---------------------------------------------------------------- self-verification
    def _self_verify(self, h: dict[str, str], raw_body: bytes, webhook_id: str) -> None:
        if not HAVE_CRYPTOGRAPHY:
            raise WebhookConfigError(
                "webhook self-verification needs the 'cryptography' package")
        try:
            signature = base64.b64decode(h[H_TRANSMISSION_SIG], validate=True)
        except (binascii.Error, ValueError):
            raise WebhookVerificationError("signature rejected") from None
        cert = self._certificate(h[H_CERT_URL])
        key = cert.public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise WebhookVerificationError("certificate key type rejected")
        message = signed_message(h[H_TRANSMISSION_ID], h[H_TRANSMISSION_TIME], webhook_id,
                                 raw_body)
        try:
            key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise WebhookVerificationError("signature rejected") from None

    def _bounded(self, method: str, url: str, limit: int, failure: str, **kwargs):
        """E2E-H3-3: stream an upstream response and stop reading as soon as it exceeds
        `limit` bytes; never buffer an unbounded body before deciding to trust it."""
        try:
            with self._http.stream(method, url, follow_redirects=False,
                                   timeout=self._timeout, **kwargs) as resp:
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise WebhookVerificationError(failure)
                return resp.status_code, bytes(data)
        except httpx.HTTPError:
            raise WebhookVerificationError(failure) from None

    def _certificate(self, url: str):
        from .paypal_certificates import validate_signing_chain

        with self._lock:
            pem = self._certs.get(url)
        if pem is None:
            status, pem = self._bounded("GET", url, MAX_CERT_BYTES, "certificate fetch failed")
            if status != 200:
                raise WebhookVerificationError("certificate fetch failed")
        now = self._now()
        try:
            cert = x509.load_pem_x509_certificate(pem)
        except ValueError:
            raise WebhookVerificationError("certificate unreadable") from None
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise WebhookVerificationError("certificate outside its validity period")
        try:
            cert = validate_signing_chain(pem, identity=self._config.webhook_signing_identity,
                now=now, intermediates_file=self._config.webhook_intermediates_file,
                roots=self._trust_store)
        except Exception as exc:
            # The cert id is public PayPal data already pinned by _CERT_PATH_RE; the
            # validator's message names only public certificate subjects.
            logger.warning("paypal webhook certificate chain rejected cert_id=%s detail=%s",
                           url.rsplit("/", 1)[-1][:120], str(exc)[:300])
            raise WebhookVerificationError("certificate chain or signing identity rejected") from None
        with self._lock:
            if url not in self._certs and len(self._certs) >= _MAX_CACHED_CERTS:
                self._certs.pop(next(iter(self._certs)))
            self._certs[url] = pem
        return cert

    # ---------------------------------------------------------------- capture -> order
    def _capture_order_id(self, capture_id: str) -> str | None:
        """Live PayPal refund events name only the refunded capture (``links[rel=up]``).
        Resolve its order with an AUTHENTICATED read of OUR capture; the URL is built from
        the validated id, never from the event's href. Runs only after verification."""
        try:
            token = self._token()
        except Exception:  # noqa: BLE001 - no read means no order, never a guess
            raise WebhookVerificationError("capture lookup unavailable") from None
        code, raw = self._bounded("GET", f"{SANDBOX_BASE_URL}/v2/payments/captures/{capture_id}",
                                  MAX_VERIFY_RESPONSE_BYTES, "capture lookup unavailable",
                                  headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/json"})
        if code != 200:
            raise WebhookVerificationError("capture lookup unavailable")
        try:
            data = json.loads(raw)
        except ValueError:
            raise WebhookVerificationError("capture lookup unavailable") from None
        if not isinstance(data, dict) or _str_id(data.get("id")) != capture_id:
            raise WebhookVerificationError("capture lookup unavailable")
        return _str_id(_related_ids(data).get("order_id"))

    # ---------------------------------------------------------------- postback
    def _postback(self, h: dict[str, str], raw_body: bytes, webhook_id: str) -> None:
        try:
            parsed = json.loads(raw_body)
        except ValueError:
            raise WebhookVerificationError("body is not JSON") from None
        if not isinstance(parsed, dict):
            raise WebhookVerificationError("body is not a JSON object")
        fields = {"auth_algo": h[H_AUTH_ALGO], "cert_url": h[H_CERT_URL],
                  "transmission_id": h[H_TRANSMISSION_ID],
                  "transmission_sig": h[H_TRANSMISSION_SIG],
                  "transmission_time": h[H_TRANSMISSION_TIME], "webhook_id": webhook_id}
        # PayPal: the event must be posted back "exactly as it was received" -> splice the
        # raw bytes in rather than re-serializing the parsed object.
        prefix = json.dumps(fields, separators=(",", ":"))[:-1] + ',"webhook_event":'
        body = prefix.encode() + raw_body + b"}"
        try:
            token = self._token()
        except Exception:  # noqa: BLE001 - ProviderError etc.; never a pass
            raise WebhookVerificationError("postback verification unavailable") from None
        code, raw = self._bounded("POST", SANDBOX_BASE_URL + VERIFY_PATH,
                                  MAX_VERIFY_RESPONSE_BYTES, "postback verification unavailable",
                                  content=body,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json",
                                           "Accept": "application/json"})
        if code != 200:
            raise WebhookVerificationError("postback verification failed")
        try:
            status = json.loads(raw).get("verification_status")
        except (ValueError, AttributeError):
            status = None
        if status != "SUCCESS":
            raise WebhookVerificationError("postback verification failed")


# ------------------------------------------------------------------------- parsing
def _related_ids(resource: dict) -> dict:
    sup = resource.get("supplementary_data")
    rel = sup.get("related_ids") if isinstance(sup, dict) else None
    return rel if isinstance(rel, dict) else {}


def _str_id(value: Any) -> str | None:
    if isinstance(value, str) and 1 <= len(value) <= 128 and re.match(r"^[A-Za-z0-9-]+$", value):
        return value
    return None


_UP_CAPTURE_RE = re.compile(
    r"^https://api(?:-m)?\.sandbox\.paypal\.com/v2/payments/captures/([A-Za-z0-9-]{1,128})$")


def _up_capture_id(resource: dict) -> str | None:
    """The refunded capture from a refund's ``links[rel=up]`` (exactly one, sandbox host)."""
    links = resource.get("links")
    ups = [link.get("href") for link in links if isinstance(link, dict)
           and link.get("rel") == "up"] if isinstance(links, list) else []
    if len(ups) != 1 or not isinstance(ups[0], str):
        return None
    match = _UP_CAPTURE_RE.match(ups[0])
    return match.group(1) if match else None


def parse_webhook_event(raw_body: bytes, *, received_at: datetime,
                        capture_order_lookup: Callable[[str], str | None] | None = None
                        ) -> ProviderEvent:
    """Parse an ALREADY VERIFIED delivery into a ``ProviderEvent(verified=True)``.
    Only call after verification succeeded; unknown event types raise."""
    try:
        event = json.loads(raw_body)
    except ValueError:
        raise WebhookVerificationError("body is not JSON") from None
    if not isinstance(event, dict):
        raise WebhookVerificationError("body is not a JSON object")
    event_id, event_type = event.get("id"), event.get("event_type")
    resource = event.get("resource")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise WebhookVerificationError("event id missing")
    if not isinstance(event_type, str) or event_type not in EVENT_KINDS:
        raise UnsupportedEventType("unsupported event type")
    if not isinstance(resource, dict):
        raise WebhookVerificationError("event resource missing")
    kind, fixed_status = EVENT_KINDS[event_type]
    related = _related_ids(resource)
    capture_id = None
    if kind == "order":
        order_id = _str_id(resource.get("id"))
        units = resource.get("purchase_units")
        unit = units[0] if isinstance(units, list) and units and isinstance(units[0], dict) else {}
        payments = unit.get("payments") if isinstance(unit.get("payments"), dict) else {}
        captures = payments.get("captures") if isinstance(payments.get("captures"), list) else []
        capture = captures[0] if len(captures) == 1 and isinstance(captures[0], dict) else None
        if fixed_status is not None:
            status = fixed_status
        elif len(captures) > 1:
            status = ProviderStatus.unknown
        else:
            status = map_order_status(resource.get("status") if isinstance(
                resource.get("status"), str) else None,
                capture.get("status") if capture and isinstance(capture.get("status"), str)
                else None)
        if capture is not None:
            capture_id = _str_id(capture.get("id"))
            amount, currency = parse_money(capture.get("amount"))
        else:
            amount, currency = parse_money(unit.get("amount"))
        if status is ProviderStatus.refunded:
            amount, currency = None, None   # refunded amount not in this representation
    elif kind == "capture":
        order_id = _str_id(related.get("order_id"))
        capture_id = _str_id(resource.get("id"))
        status = fixed_status or ProviderStatus.unknown
        own = resource.get("status")
        if isinstance(own, str) and CAPTURE_STATUS_MAP.get(own, ProviderStatus.unknown) != status:
            # event name and the capture's own status disagree: claim nothing
            status = ProviderStatus.unknown
        amount, currency = parse_money(resource.get("amount"))
    elif kind == "refund":
        # resource is the refund/reversal; amount is the REFUNDED amount (lead decision 2)
        order_id = _str_id(related.get("order_id"))
        capture_id = _str_id(related.get("capture_id"))
        if capture_id is None:
            capture_id = _up_capture_id(resource)
        if order_id is None and capture_id is not None and capture_order_lookup is not None:
            order_id = capture_order_lookup(capture_id)
        status = fixed_status or ProviderStatus.unknown
        amount, currency = parse_money(resource.get("amount"))
        if amount is not None:
            amount = abs(amount)
    else:  # approval_reversed
        order_id = _str_id(resource.get("order_id")) or _str_id(related.get("order_id"))
        status = fixed_status or ProviderStatus.unknown
        amount, currency = None, None
    if order_id is None:
        raise WebhookVerificationError("event does not name an order")
    return ProviderEvent(
        provider=PROVIDER_NAME, environment="sandbox", event_id=event_id,
        event_type=event_type, provider_order_id=order_id, provider_capture_id=capture_id,
        status=status, amount=amount, currency=currency, verified=True,
        occurred_at=parse_time(event.get("create_time")), received_at=received_at,
        raw_sha256=hashlib.sha256(raw_body).hexdigest())
