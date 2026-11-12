"""Async clients for the owner and agent API surfaces. No automatic retries."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .errors import (
    AgentPalError,
    AuthenticationError,
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    TransportError,
    ValidationError,
)

Amount = Decimal | str
_AMOUNT = re.compile(r"^[0-9]{1,10}(\.[0-9]{1,8})?$")
_AMOUNT_FIELDS = frozenset({"amount", "monthly_budget", "per_transaction_limit",
                            "per_transaction_review_limit", "rolling_window_limit"})
_ERROR_TYPES = {400: ValidationError, 401: AuthenticationError, 403: PermissionDeniedError,
                404: NotFoundError, 409: ConflictError, 422: ValidationError,
                429: RateLimitError, 502: ProviderError, 503: NotConfiguredError}


def _amount(value: Amount) -> str:
    if not isinstance(value, (Decimal, str)):
        raise TypeError("amounts must be Decimal or str; floats are refused")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("amount must be finite")
        value = format(value, "f")
    if not _AMOUNT.fullmatch(value):
        raise ValueError("amount must be a nonnegative plain decimal string")
    return value


def _amounts(body: dict[str, Any]) -> dict[str, Any]:
    return {k: _amount(v) if k in _AMOUNT_FIELDS else v
            for k, v in body.items()}


def _id(value: str) -> str:
    return quote(value, safe="")


class _Client:
    _prefix: str

    def __init__(self, *, base_url: str, credential: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30.0):
        if not credential.startswith(self._prefix):
            raise ValueError("wrong credential type")
        # Each instance owns a separate transport client; credentials cannot bleed
        # between an owner client and an agent client through shared default headers.
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        self._http = httpx.AsyncClient(
            base_url=base_url + "/", transport=transport, timeout=timeout,
            follow_redirects=False, headers={"Authorization": f"Bearer {credential}"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, body: dict | None = None,
                       idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = await self._http.request(method, "v1/" + path, json=body,
                                                headers=headers)
        except httpx.RequestError:
            raise TransportError("transport_error", idempotency_key=idempotency_key) from None
        if not 200 <= response.status_code < 300:
            code = "api_error"
            try:
                candidate = response.json().get("error")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z_]{0,63}", candidate):
                    code = candidate
            except (ValueError, AttributeError):
                pass
            error_type = _ERROR_TYPES.get(response.status_code, AgentPalError)
            raise error_type(code, status_code=response.status_code,
                             idempotency_key=idempotency_key)
        return None if response.status_code == 204 else response.json()

    async def health(self) -> dict:
        return await self._request("GET", "health")

    async def list_payment_requests(self) -> dict:
        return await self._request("GET", "payment-requests")

    async def get_payment_request(self, request_id: str) -> dict:
        return await self._request("GET", f"payment-requests/{_id(request_id)}")


class OwnerClient(_Client):
    """Owner credential (`apo_…`): manage agents, budgets, and human decisions."""

    _prefix = "apo_"

    async def register_agent(self, *, name: str, **options) -> dict:
        return await self._request("POST", "agents", body={"name": name, **options})

    async def list_agents(self) -> dict:
        return await self._request("GET", "agents")

    async def get_agent(self, agent_id: str) -> dict:
        return await self._request("GET", f"agents/{_id(agent_id)}")

    async def rotate_agent_credential(self, agent_id: str, *, overlap_seconds: int = 0) -> dict:
        return await self._request("POST", f"agents/{_id(agent_id)}/credentials",
                                   body={"overlap_seconds": overlap_seconds})

    async def revoke_agent_credential(self, agent_id: str, key_id: str) -> None:
        await self._request("DELETE", f"agents/{_id(agent_id)}/credentials/{_id(key_id)}")

    async def suspend_agent(self, agent_id: str) -> dict:
        return await self._request("POST", f"agents/{_id(agent_id)}/suspend")

    async def reactivate_agent(self, agent_id: str) -> dict:
        return await self._request("POST", f"agents/{_id(agent_id)}/reactivate")

    async def get_agent_permissions(self, agent_id: str) -> dict:
        return await self._request("GET", f"agents/{_id(agent_id)}/permissions")

    async def set_agent_permissions(self, agent_id: str, capabilities: list[str]) -> dict:
        return await self._request("PUT", f"agents/{_id(agent_id)}/permissions",
                                   body={"capabilities": capabilities})

    async def create_wallet(self, *, agent_id: str, currency: str,
                            monthly_budget: Amount, per_transaction_limit: Amount,
                            **options) -> dict:
        body = _amounts({"agent_id": agent_id, "currency": currency,
                         "monthly_budget": monthly_budget,
                         "per_transaction_limit": per_transaction_limit, **options})
        return await self._request("POST", "wallets", body=body)

    async def list_wallets(self) -> dict:
        return await self._request("GET", "wallets")

    async def get_wallet(self, wallet_id: str) -> dict:
        return await self._request("GET", f"wallets/{_id(wallet_id)}")

    async def update_wallet(self, wallet_id: str, **limits: Amount) -> dict:
        return await self._request("PATCH", f"wallets/{_id(wallet_id)}", body=_amounts(limits))

    async def freeze_wallet(self, wallet_id: str) -> dict:
        return await self._request("POST", f"wallets/{_id(wallet_id)}/freeze")

    async def unfreeze_wallet(self, wallet_id: str) -> dict:
        return await self._request("POST", f"wallets/{_id(wallet_id)}/unfreeze")

    async def list_wallet_transactions(self, wallet_id: str) -> dict:
        return await self._request("GET", f"wallets/{_id(wallet_id)}/transactions")

    async def list_approvals(self) -> dict:
        return await self._request("GET", "approvals")

    async def approve_payment_request(self, request_id: str, *, terms_sha256: str) -> dict:
        return await self._request("POST", f"payment-requests/{_id(request_id)}/approve",
                                   body={"terms_sha256": terms_sha256})

    async def reject_payment_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payment-requests/{_id(request_id)}/reject")

    async def counter_payment_request(self, request_id: str, *, deal_index: int | None = None,
                                      amount: Amount | None = None,
                                      merchant_name: str | None = None,
                                      merchant_domain: str | None = None,
                                      merchant_paypal_email: str | None = None) -> dict:
        """Owner counter-offer ("buy this instead"). Approves nothing: the request is
        declined and a replacement with these terms is created for the same agent through
        the normal request path. Returns `{original, replacement}`; approve the
        replacement's own `terms_sha256` with `approve_payment_request`. Give `deal_index`
        (a stored Channel3 better deal; the server supplies its price and retailer) or
        `amount` and `merchant_name`."""
        body: dict[str, Any] = {}
        if deal_index is not None:
            body["deal_index"] = deal_index
        if amount is not None:
            body["amount"] = _amount(amount)
        for name, value in (("merchant_name", merchant_name),
                            ("merchant_domain", merchant_domain),
                            ("merchant_paypal_email", merchant_paypal_email)):
            if value is not None:
                body[name] = value
        return await self._request("POST", f"payment-requests/{_id(request_id)}/counter",
                                   body=body)

    async def get_reconciliation(self, request_id: str) -> dict:
        return await self._request("GET", f"payment-requests/{_id(request_id)}/reconciliation")

    async def capture_payment_request(self, request_id: str) -> dict:
        """Owner: capture after the sandbox buyer approved on PayPal. AgentPal counts it as
        completed only after a verified PayPal outcome."""
        return await self._request("POST", f"payment-requests/{_id(request_id)}/capture")

    async def reconcile_payment_request(self, request_id: str) -> dict:
        """Owner: re-read PayPal (verified read only) for an uncertain outcome."""
        return await self._request("POST", f"payment-requests/{_id(request_id)}/reconciliation")

    async def refresh_market_check(self, request_id: str) -> dict:
        """Owner: refresh the Channel3 market check with live offers (advisory only)."""
        return await self._request("POST", f"payment-requests/{_id(request_id)}/market-check")

    async def list_payout_requests(self) -> dict:
        return await self._request("GET", "payout-requests")

    async def get_payout_request(self, request_id: str) -> dict:
        return await self._request("GET", f"payout-requests/{_id(request_id)}")

    async def approve_payout_request(self, request_id: str, *, terms_sha256: str) -> dict:
        return await self._request("POST", f"payout-requests/{_id(request_id)}/approve",
                                   body={"terms_sha256": terms_sha256})

    async def reject_payout_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payout-requests/{_id(request_id)}/reject")

    async def reconcile_payout_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payout-requests/{_id(request_id)}/reconciliation")


    async def reconcile_with_paypal(self, *, start: str, end: str) -> dict:
        """Owner-only, read-only PayPal comparison (at most 31 days)."""
        from urllib.parse import urlencode
        return await self._request("GET", "paypal/reconciliation?" + urlencode({"start": start, "end": end}))
    async def refund_payment(self, request_id: str, *, amount: Amount | None = None,
                             reason: str | None = None,
                             idempotency_key: str | None = None) -> dict:
        body = {}
        if amount is not None:
            body["amount"] = _amount(amount)
        if reason is not None:
            if not isinstance(reason, str) or len(reason) > 255:
                raise ValueError("refund reason must be at most 255 characters")
            body["reason"] = reason
        return await self._request("POST", f"payment-requests/{_id(request_id)}/refunds",
                                   body=body, idempotency_key=uuid4().hex
                                   if idempotency_key is None else idempotency_key)

    async def list_payment_refunds(self, request_id: str) -> dict:
        return await self._request("GET", f"payment-requests/{_id(request_id)}/refunds")

    async def reconcile_payment_refunds(self, request_id: str) -> dict:
        return await self._request("POST", f"payment-requests/{_id(request_id)}/refunds/reconciliation")

    async def list_disputes(self) -> dict:
        return await self._request("GET", "disputes")

    async def check_merchant_page(self, request_id: str, url: str | None = None) -> dict:
        """Owner: visit the merchant page before deciding; advisory evidence only."""
        return await self._request("POST", f"payment-requests/{_id(request_id)}/page-check",
                                   body={"url": url} if url is not None else None)


class AgentClient(_Client):
    """Agent key (`apk_…`): own identity, virtual allowance, and payment requests."""

    _prefix = "apk_"

    async def browse(self, url: str) -> dict:
        """Read-only isolated browser visit; needs the owner-granted research.fetch."""
        return await self._request("POST", "research/browse", body={"url": url})

    async def get_me(self) -> dict:
        return await self._request("GET", "me")

    async def get_wallet(self) -> dict:
        return await self._request("GET", "me/wallet")

    async def list_transactions(self) -> dict:
        return await self._request("GET", "me/wallet/transactions")

    async def create_payment_request(self, *, amount: Amount, currency: str,
                                     merchant_name: str, purpose: str,
                                     idempotency_key: str | None = None, **options) -> dict:
        body = _amounts({"amount": amount, "currency": currency,
                         "merchant_name": merchant_name, "purpose": purpose, **options})
        return await self._request("POST", "payment-requests", body=body,
                                   idempotency_key=uuid4().hex if idempotency_key is None else idempotency_key)

    async def request_owner_decision(self, request_id: str) -> dict:
        return await self._request("POST", f"payment-requests/{_id(request_id)}/request-decision")

    async def execute_payment_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payment-requests/{_id(request_id)}/execute")

    async def create_payout_request(self, *, receiver_email: str, amount: Amount, currency: str,
                                    purpose: str, note: str = "",
                                    idempotency_key: str | None = None) -> dict:
        body = _amounts({"receiver_email": receiver_email, "amount": amount, "currency": currency,
                         "purpose": purpose, "note": note})
        return await self._request("POST", "payout-requests", body=body,
            idempotency_key=uuid4().hex if idempotency_key is None else idempotency_key)

    async def execute_payout_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payout-requests/{_id(request_id)}/execute")

    async def reconcile_payout_request(self, request_id: str) -> dict:
        return await self._request("POST", f"payout-requests/{_id(request_id)}/reconciliation")
