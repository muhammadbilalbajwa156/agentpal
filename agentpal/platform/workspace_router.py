"""Setup, workspace integrations and owner-only Test Lab routes."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .errors import not_configured, PlatformError
from .integrations import CATALOG, VerificationFailed
from .lab import IntegrationRequired
from .models import IDEMPOTENCY_KEY_PATTERN, contains_credential
from .router import Owner, RequestId, _platform, _E, _E409
from .workspace_models import (IntegrationInput, IntegrationItem, IntegrationList, LabAgent,
    LabErrand, LabErrandOut, LabPayment, LabPaymentCreate, SetupOwner, SetupOwnerOut,
    SetupStatus, VerificationError)


def build_workspace_router():
    r = APIRouter(prefix="/v1")

    def setup(request):
        service = _platform(request).deps.setup
        if service is None:
            raise not_configured()
        return service

    def integrations(request):
        service = _platform(request).deps.integrations
        if service is None:
            raise not_configured()
        return service

    def lab(request):
        p = _platform(request)
        if p.lab is None:
            raise not_configured()
        return p.lab

    @r.get("/setup/status", response_model=SetupStatus, operation_id="getSetupStatus", tags=["setup"], openapi_extra={"security": []})
    def setup_status(request: Request):
        return setup(request).status()

    @r.post("/setup/owner", response_model=SetupOwnerOut, status_code=201, operation_id="setupOwner", tags=["setup"], openapi_extra={"security": []}, responses=_E409)
    def setup_owner(body: SetupOwner, request: Request):
        peer = request.client.host if request.client else ""
        return setup(request).signup(body.owner_id, body.display_name, body.code, peer)

    @r.get("/integrations", response_model=IntegrationList, operation_id="listIntegrations", tags=["integrations"], responses=_E)
    def list_integrations(owner: Owner, request: Request):
        service = integrations(request)
        return {"integrations": [service.item(owner.owner_id, name) for name in CATALOG]}

    def verified(call):
        try:
            return call()
        except VerificationFailed as exc:
            return JSONResponse(status_code=422, content={"error": "verification_failed", "error_code": exc.code})

    @r.put("/integrations/{provider}", response_model=IntegrationItem, operation_id="putIntegration", tags=["integrations"], responses={**_E, 422: {"model": VerificationError}})
    def put_integration(provider: str, body: IntegrationInput, owner: Owner, request: Request):
        service = integrations(request)
        return verified(lambda: service.put(owner.owner_id, provider,
            {name: value.get_secret_value() for name, value in body.fields.items()}))

    @r.post("/integrations/{provider}/verify", response_model=IntegrationItem, operation_id="verifyIntegration", tags=["integrations"], responses={**_E409, 422: {"model": VerificationError}})
    def verify_integration(provider: str, owner: Owner, request: Request):
        return verified(lambda: integrations(request).verify(owner.owner_id, provider))

    @r.delete("/integrations/{provider}", status_code=204, operation_id="deleteIntegration", tags=["integrations"], responses=_E)
    def delete_integration(provider: str, owner: Owner, request: Request):
        integrations(request).delete(owner.owner_id, provider)
        return Response(status_code=204)

    @r.post("/lab/agent", response_model=LabAgent, operation_id="createLabAgent", tags=["lab"], responses=_E409)
    def lab_agent(owner: Owner, request: Request):
        return lab(request).agent(owner)

    @r.post("/lab/payment-requests", response_model=LabPayment, status_code=201, operation_id="createLabPayment", tags=["lab"], responses=_E409)
    def lab_payment(body: LabPaymentCreate, owner: Owner, request: Request):
        import re
        key = request.headers.get("Idempotency-Key")
        if key and (not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key) or contains_credential(key)):
            raise PlatformError(422, "invalid_request")
        return lab(request).create(owner, body, key)

    @r.post("/lab/payment-requests/{request_id}/execute", response_model=LabPayment, operation_id="executeLabPayment", tags=["lab"], responses=_E409)
    def lab_execute(request_id: RequestId, owner: Owner, request: Request):
        return lab(request).execute(owner, request_id)

    @r.post("/lab/errands", response_model=LabErrandOut, operation_id="createLabErrand", tags=["lab"], responses=_E409)
    def lab_errand(body: LabErrand, owner: Owner, request: Request):
        try:
            return lab(request).errand(owner, body.task, body.budget)
        except IntegrationRequired as exc:
            return JSONResponse(status_code=409, content={"error": "integration_required", "provider": exc.provider})

    return r
