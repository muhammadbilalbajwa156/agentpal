"""Shared self-hosting contract. Secret input has no corresponding output value."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .models import Amount, PaymentCreate, PaymentOut, _In


class SetupStatus(BaseModel):
    mode: Literal["self_hosted", "hosted"]
    needs_setup: bool
    signup: Literal["setup_code", "invite_code", "disabled"]


class SetupOwner(_In):
    owner_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    display_name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=512, repr=False)


class SetupOwnerOut(BaseModel):
    owner_id: str
    token: str


class IntegrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, SecretStr] = Field(max_length=3, repr=False)


class IntegrationField(BaseModel):
    name: str
    secret: bool
    required: bool


class HostedLimits(BaseModel):
    max_amount: str
    max_monthly_budget: str


class IntegrationItem(BaseModel):
    provider: str
    label: str
    purpose: str
    required: Literal[False] = False
    fields: list[IntegrationField]
    status: Literal["not_configured", "verified", "failed"]
    source: Literal["workspace", "server"] | None
    hint: str | None = None
    verified_at: str | None = None
    error_code: str | None = None
    hosted_limits: HostedLimits | None = None


class IntegrationList(BaseModel):
    integrations: list[IntegrationItem]


class VerificationError(BaseModel):
    error: Literal["verification_failed"] = "verification_failed"
    error_code: str


class LabAgent(BaseModel):
    agent_id: str
    wallet_id: str


class LabPayment(PaymentOut):
    payer_action_url: str | None = None
    provider_mode: Literal["simulated", "paypal_sandbox"]


class LabPaymentCreate(PaymentCreate):
    request_owner_decision: Literal[True] = True


class LabErrand(_In):
    task: str = Field(min_length=1, max_length=500)
    budget: Amount = Field(default="30.00", gt=0, le=50)


class LabScenarioOut(BaseModel):
    scenario: Literal["over_limit", "split_purchase", "changed_terms", "payee_swap"]
    outcome: Literal["blocked", "refused"]
    rule_codes: list[str]
    plain_reason: str
    request_ids: list[str]
    timeline_url_hint: str


class LabEvent(BaseModel):
    at: str
    message: str
    kind: Literal["plan", "candidate", "selection", "boundary"] | None = None
    query: str | None = None
    max_price: str | None = None
    title: str | None = None
    amount: str | None = None
    currency: str | None = None
    merchant: str | None = None
    in_stock: bool | None = None
    justification: str | None = None


class LabErrandOut(BaseModel):
    task: str
    status: str
    request_id: str | None
    events: list[LabEvent]
