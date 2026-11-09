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


class LabEvent(BaseModel):
    at: str
    message: str


class LabErrandOut(BaseModel):
    status: str
    request_id: str | None
    events: list[LabEvent]
