"""Single-item PayPal Sandbox Payouts. Only authenticated READs establish outcomes.

No retries, redirects, caller-supplied URLs, or raw provider errors. Both OAuth and
payout responses are streamed with a hard size bound. Existing TLS/proxy settings apply.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time

import httpx

from ..infra_contracts import ProviderError
from .config import PayPalSandboxConfig, SANDBOX_BASE_URL, assert_sandbox_base_url

MAX_RESPONSE_BYTES = 128 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,30}")
_MONEY = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,8})?")
PATH = "/v1/payments/payouts"
PROVIDER = "paypal_payouts_sandbox"


def sender_batch_id(request_id: str) -> str:
    return "ap_" + hashlib.sha256(("payout-batch|" + request_id).encode()).hexdigest()[:27]


def payout_request_id(request_id: str) -> str:
    return "payout-" + hashlib.sha256(("payout-create|" + request_id).encode()).hexdigest()[:31]


def _identifier(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProviderError("payout_response_mismatch", outcome_unknown=True)
    return value


@dataclass(frozen=True)
class PayoutObservation:
    batch_id: str
    item_id: str
    transaction_status: str
    raw_sha256: str


class PayPalPayoutsProvider:
    environment = "sandbox"
    name = PROVIDER

    def __init__(self, config: PayPalSandboxConfig, *, http_client: httpx.Client):
        if not isinstance(config, PayPalSandboxConfig):
            raise TypeError("sandbox configuration required")
        self._base = assert_sandbox_base_url(config.base_url)
        self._config = config
        self._http = http_client
        self._timeout = httpx.Timeout(config.read_timeout_s, connect=config.connect_timeout_s)

    def __repr__(self):
        return "PayPalPayoutsProvider(environment='sandbox')"

    def _request(self, method, path, *, headers, content, mutating):
        # Fixed API origin and bounded, internal path components only.
        if self._base != SANDBOX_BASE_URL:
            raise ProviderError("payout_sandbox_required", outcome_unknown=False)
        started = time.monotonic()
        try:
            with self._http.stream(method, self._base + path, headers=headers, content=content,
                                   timeout=self._timeout, follow_redirects=False) as response:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if time.monotonic() - started > self._config.read_timeout_s:
                        raise ProviderError("payout_response_deadline", outcome_unknown=mutating)
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ProviderError("payout_response_too_large", outcome_unknown=mutating)
                status = response.status_code
        except httpx.HTTPError:
            # Even connect errors are conservatively unknown for a money mutation.
            raise ProviderError("payout_transport_error", outcome_unknown=mutating) from None
        if not 200 <= status < 300:
            raise ProviderError("payout_provider_error",
                                outcome_unknown=mutating and (status < 400 or status >= 500
                                                              or status in (400, 409)))
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise ProviderError("payout_invalid_response", outcome_unknown=mutating) from None
        if not isinstance(data, dict):
            raise ProviderError("payout_invalid_response", outcome_unknown=mutating)
        return data, bytes(raw)

    def _token(self):
        basic = base64.b64encode((self._config.client_id + ":" +
                                 self._config.client_secret.get_secret_value()).encode()).decode()
        data, _ = self._request("POST", "/v1/oauth2/token", headers={
            "Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
            content=b"grant_type=client_credentials", mutating=False)
        token = data.get("access_token")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,8192}={0,2}", token)
                or str(data.get("token_type", "")).lower() != "bearer"):
            raise ProviderError("payout_oauth_invalid", outcome_unknown=False)
        return token

    def create(self, terms: dict) -> str:
        batch = sender_batch_id(terms["request_id"])
        item = {"recipient_type": "EMAIL", "receiver": terms["receiver_email"],
                "amount": {"value": terms["amount"], "currency": terms["currency"]},
                "sender_item_id": batch}
        if terms["note"]:
            item["note"] = terms["note"]
        body = {"sender_batch_header": {"sender_batch_id": batch,
            "email_subject": "AgentPal sandbox payout"}, "items": [item]}
        data, _ = self._request("POST", PATH, headers={"Authorization": "Bearer " + self._token(),
            "Content-Type": "application/json", "PayPal-Request-Id": payout_request_id(terms["request_id"])},
            content=json.dumps(body, separators=(",", ":")).encode(), mutating=True)
        # A POST acceptance/status can never settle a payout. Persist its id, then READ.
        header = data.get("batch_header")
        if not isinstance(header, dict):
            raise ProviderError("payout_invalid_response", outcome_unknown=True)
        return _identifier(header.get("payout_batch_id"))

    def read(self, batch_id: str, terms: dict) -> PayoutObservation:
        batch_id = _identifier(batch_id)
        data, raw = self._request("GET", PATH + "/" + batch_id,
            headers={"Authorization": "Bearer " + self._token()}, content=None, mutating=False)
        try:
            header = data["batch_header"]
            items = data["items"]
            if (header["payout_batch_id"] != batch_id
                    or header["sender_batch_header"]["sender_batch_id"] != sender_batch_id(terms["request_id"])
                    or not isinstance(items, list) or len(items) != 1
                    or data.get("total_items", 1) != 1 or data.get("total_pages", 1) != 1):
                raise ValueError
            item = items[0]
            payload = item["payout_item"]
            money = payload["amount"]
            value = money["value"]
            # PayPal's Payouts v1 currency member is `currency`, not Orders v2's currency_code.
            if (item["payout_batch_id"] != batch_id or payload["recipient_type"] != "EMAIL"
                    or payload["receiver"].lower() != terms["receiver_email"]
                    or payload["sender_item_id"] != sender_batch_id(terms["request_id"])
                    or payload.get("note", "") != terms["note"]
                    or money["currency"] != terms["currency"]
                    or not isinstance(value, str) or not _MONEY.fullmatch(value)
                    or Decimal(value) != Decimal(terms["amount"])):
                raise ValueError
            status = item["transaction_status"]
            if status not in {"SUCCESS", "PENDING", "ONHOLD", "UNCLAIMED", "FAILED", "RETURNED",
                              "BLOCKED", "REFUNDED", "DENIED", "REVERSED"}:
                raise ValueError
            item_id = _identifier(item["payout_item_id"])
        except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
            raise ProviderError("payout_response_mismatch", outcome_unknown=True) from None
        return PayoutObservation(batch_id, item_id, status, hashlib.sha256(raw).hexdigest())
