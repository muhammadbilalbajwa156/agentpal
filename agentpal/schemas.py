"""Typed schemas: the incoming payment request and the produced risk assessment.

Everything an agent supplies is validated and normalized here. Free-text fields
are treated as *untrusted data*: they are length-limited and stored as-is, never
interpreted as instructions anywhere in the pipeline.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    field_validator,
    model_validator,
)

# ISO 4217 minor units for the currencies we are willing to parse at all.
# Whether a currency is *allowed* is a policy decision (see risk_policy.json).
CURRENCY_MINOR_UNITS: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "JPY": 0, "CHF": 2,
    "INR": 2, "PKR": 2, "SGD": 2, "NZD": 2, "SEK": 2, "NOK": 2, "DKK": 2,
    "MXN": 2, "BRL": 2, "ZAR": 2, "AED": 2, "HKD": 2, "CNY": 2,
}

MAX_AMOUNT = Decimal("1000000000")
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{2,127}$"
_DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

RequestId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


def normalize_domain(value: str) -> str:
    """Lower-case, strip scheme/path/port/'www.', convert IDNs to punycode."""
    raw = value.strip().lower()
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in raw:
        raise ValueError("merchant_domain must not contain credentials")
    host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError("merchant_domain is empty")
    try:
        ipaddress.ip_address(host)
        return host  # kept as-is; flagged later as a risk indicator
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("merchant_domain is not a valid hostname") from exc
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253:
        raise ValueError("merchant_domain must be a fully qualified domain name")
    if not all(_DOMAIN_LABEL.match(label) for label in labels):
        raise ValueError("merchant_domain contains invalid characters")
    if labels[-1].isdigit():
        raise ValueError("merchant_domain has an invalid top-level domain")
    return host


class Direction(str, Enum):
    outgoing = "outgoing"
    incoming = "incoming"


class BillingInterval(str, Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"
    annually = "annually"


class EvidenceType(str, Enum):
    invoice = "invoice"
    quote = "quote"
    contract = "contract"
    receipt = "receipt"
    url = "url"
    note = "note"
    other = "other"


class Evidence(BaseModel):
    """A piece of supporting material *claimed* by the agent. Not verified."""

    model_config = ConfigDict(extra="forbid")
    type: EvidenceType
    description: LongText = ""
    reference: ShortText | None = None
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT)
    currency: str | None = None
    merchant_domain: str | None = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if v not in CURRENCY_MINOR_UNITS:
            raise ValueError("unsupported currency code")  # never echo input (P0.7c)
        return v

    @field_validator("merchant_domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        return normalize_domain(v) if v else None


class PaymentRequest(BaseModel):
    """A structured spend (or, later, incoming-payment) request from an AI agent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: RequestId
    agent_id: RequestId
    direction: Direction = Direction.outgoing
    amount: Decimal
    currency: str
    merchant_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    merchant_domain: str | None = None
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    business_justification: LongText = ""
    recurring: bool = False
    billing_interval: BillingInterval | None = None
    monthly_budget: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT)
    budget_spent_this_period: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    previous_payment_reference: ShortText | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    created_at: datetime | None = None
    expires_at: datetime | None = None

    # Original merchant_domain text before normalization, kept for audit only (P0.6).
    _merchant_domain_input: str | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _keep_raw_domain(cls, data, handler):
        instance = handler(data)
        if isinstance(data, dict) and data.get("merchant_domain") is not None:
            instance._merchant_domain_input = str(data["merchant_domain"])[:300]
        return instance

    @property
    def merchant_domain_input(self) -> str | None:
        return self._merchant_domain_input

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_type(cls, v: object) -> object:
        if isinstance(v, bool) or v is None:
            raise ValueError("amount must be a number")
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            raise ValueError("amount must be finite")
        if isinstance(v, str):
            try:
                Decimal(v.strip())
            except InvalidOperation as exc:
                raise ValueError("amount must be a decimal number") from exc
        return v

    @field_validator("amount")
    @classmethod
    def _amount_range(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("amount must be finite")
        if v <= 0:
            raise ValueError("amount must be greater than zero")
        if v > MAX_AMOUNT:
            raise ValueError("amount exceeds the maximum accepted value")
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in CURRENCY_MINOR_UNITS:
            raise ValueError("unsupported currency code")  # never echo input (P0.7c)
        return v

    @field_validator("merchant_domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return normalize_domain(v)

    @field_validator("created_at", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset (e.g. 'Z' or '+00:00')")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _cross_field(self) -> "PaymentRequest":
        minor = CURRENCY_MINOR_UNITS[self.currency]
        quantum = Decimal(1).scaleb(-minor)
        if self.amount != self.amount.quantize(quantum, rounding=ROUND_HALF_UP):
            raise ValueError(
                f"amount has more decimal places than {self.currency} allows ({minor})"
            )
        self.amount = self.amount.quantize(quantum)
        if not self.recurring and self.billing_interval is not None:
            raise ValueError("billing_interval is only valid when recurring is true")
        if self.created_at and self.expires_at and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


# ---------------------------------------------------------------- outputs


class SignalKind(str, Enum):
    hard_violation = "hard_violation"  # mandatory security/policy rule; cannot be overridden
    risk_indicator = "risk_indicator"  # probabilistic fraud/abuse indicator
    uncertainty = "uncertainty"        # missing/inconsistent info; NOT evidence of fraud


class SignalSource(str, Enum):
    rule = "rule"
    model = "model"


class RiskSignal(BaseModel):
    code: str
    kind: SignalKind
    source: SignalSource
    points: int = Field(ge=0, le=100)
    message: str
    evidence: dict[str, object] = Field(default_factory=dict)


RiskCategory = Literal["low", "medium", "high", "critical"]
Recommendation = Literal["allow_for_review", "require_review", "block"]


class ModelFinding(BaseModel):
    code: str = Field(max_length=64)
    description: str = Field(max_length=500)
    evidence: str = Field(default="", max_length=500)


class ModelAnalysis(BaseModel):
    """Structured output every analyzer backend must produce (schema-validated)."""

    model_config = ConfigDict(extra="ignore")
    risk_score: int = Field(ge=0, le=100)
    findings: list[ModelFinding] = Field(default_factory=list, max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=12)
    uncertainty: float = Field(ge=0.0, le=1.0)
    recommendation: Recommendation
    summary: str = Field(default="", max_length=1000)


class ModelTrace(BaseModel):
    backend: str
    model_name: str
    model_version: str
    is_fallback: bool
    attempted_backends: list[str]
    errors: list[str] = Field(default_factory=list)
    latency_ms: float


class RiskAssessment(BaseModel):
    request_id: str
    agent_id: str
    assessed_at: datetime                     # trusted server receipt time (UTC)
    effective_expires_at: datetime            # server-computed; client value only shortens it
    risk_score: int = Field(ge=0, le=100)
    risk_category: RiskCategory
    recommendation: Recommendation
    explanation: str
    signals: list[RiskSignal]
    hard_violations: list[str]
    missing_information: list[str]
    inconsistencies: list[str]
    rule_score: int = Field(ge=0, le=100)
    model_score: int | None = Field(default=None, ge=0, le=100)
    model_uncertainty: float | None = None
    model_summary: str | None = None
    model: ModelTrace
    rules_version: str
    disclaimer: str = (
        "Automated risk assessment only. It is not proof of fraud or legitimacy, "
        "has not verified the merchant or any evidence, and does not authorize "
        "or execute any payment."
    )
    payment_executed: Literal[False] = False
    # owner decision 5: True when a configured model was not used (failure/timeout/unavailable)
    degraded: bool = False
    degraded_reason: str | None = None
