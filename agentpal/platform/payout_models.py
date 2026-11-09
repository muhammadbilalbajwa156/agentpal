"""Exact, bounded payout inputs and sandbox-only API representations."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..infra_contracts import PaymentLifecycle
from ..schemas import CURRENCY_MINOR_UNITS
from .models import Amount, AmountOut, Currency, OwnerDecisionOut, VerificationOut, _In, _Out


def receiver_email(value: str) -> str:
    """Deliberately narrow ASCII mailbox grammar: no display names, literals or controls."""
    if not isinstance(value, str) or len(value) > 254 or value.count("@") != 1:
        raise ValueError("invalid receiver email")
    local, domain = value.split("@")
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("invalid receiver email")
    labels = domain.split(".")
    if (len(labels) < 2 or not re.fullmatch(r"[A-Za-z]{2,63}", labels[-1])
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                   for p in labels)):
        raise ValueError("invalid receiver email")
    return value.lower()


class PayoutCreate(_In):
    receiver_email: str = Field(min_length=3, max_length=254)
    amount: Amount = Field(gt=0, le=Decimal("9999999999"))
    currency: Currency
    note: str = Field(default="", max_length=255)
    purpose: str = Field(min_length=1, max_length=255)

    @field_validator("receiver_email")
    @classmethod
    def _email(cls, value):
        return receiver_email(value)

    @field_validator("note", "purpose")
    @classmethod
    def _text(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def _minor_units(self):
        minor = CURRENCY_MINOR_UNITS.get(self.currency)
        if minor is None or self.amount.as_tuple().exponent < -minor:
            raise ValueError("invalid currency amount")
        self.amount = self.amount.quantize(Decimal(1).scaleb(-minor))
        return self


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


class PayoutOut(_Out):
    owner_decision: OwnerDecisionOut | None = Field(default=None, description="Owner reads only; null for agents.")
    verification: VerificationOut | None = None
    request_id: str
    agent_id: str
    owner_id: str
    wallet_id: str
    receiver_email: str
    amount: AmountOut
    currency: str
    note: str
    purpose: str
    terms_sha256: str
    status: PaymentLifecycle
    provider_batch_id: str | None = None
    provider_item_status: str | None = None
    unclaimed: bool = False
    warning: str | None = None
    created_at: str
    payment_environment: Literal["sandbox"] = "sandbox"
    is_real_money: Literal[False] = False


class PayoutList(_Out):
    payout_requests: list[PayoutOut]
