"""/v1 routes for owner-approved PayPal Payouts (workstream P3, lead-scaffolded 2026-11-02).

Only ADDS routes to the /v1 router; never changes existing routes. Import shared
dependencies (Owner, Agent, RequestId, _platform, ...) from `.router` inside the function.
"""

import re

from fastapi import APIRouter, Request, Response

from .errors import PlatformError
from .models import ApprovalDecision, ErrorOut, IDEMPOTENCY_KEY_PATTERN, TimelineOut, contains_credential
from .payout_models import PayoutCreate, PayoutList, PayoutOut
from .payouts import PayoutDesk


def add_payout_routes(r: APIRouter) -> None:
    """Register only P3's owned surface; shared dependencies enforce credential types."""
    from .router import Owner, Agent, AnyCaller, RequestId, _platform, _E503

    @r.post("/payout-requests", response_model=PayoutOut, status_code=201,
            operation_id="createPayoutRequest", tags=["payouts"],
            responses={**_E503, 200: {"model": PayoutOut, "description": "Idempotent replay",
                "headers": {"Idempotent-Replayed": {"description": "Stored create response",
                    "schema": {"type": "string", "enum": ["true"]}}}},
                       400: {"model": ErrorOut, "description": "Idempotency-Key required"}},
            openapi_extra={"parameters": [{"name": "Idempotency-Key", "in": "header",
                "required": True, "schema": {"type": "string", "pattern": IDEMPOTENCY_KEY_PATTERN}}]})
    def create_payout(body: PayoutCreate, agent: Agent, request: Request, response: Response):
        key = request.headers.get("Idempotency-Key")
        if key is None:
            raise PlatformError(400, "idempotency_key_required")
        if not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key) or contains_credential(key):
            raise PlatformError(422, "invalid_request")
        result, replay = PayoutDesk(_platform(request)).create(agent.ctx, body, key)
        if replay:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return result

    @r.get("/payout-requests", response_model=PayoutList, operation_id="listPayoutRequests",
           tags=["payouts"], responses=_E503)
    def list_payouts(owner: Owner, request: Request):
        return {"payout_requests": PayoutDesk(_platform(request)).list_owned(owner)}

    @r.get("/payout-requests/{request_id}", response_model=PayoutOut,
           operation_id="getPayoutRequest", tags=["payouts"], responses=_E503)
    def get_payout(request_id: RequestId, owner: Owner, request: Request):
        desk = PayoutDesk(_platform(request))
        return desk.view(desk.owned(owner, request_id), owner=True)

    @r.get("/payout-requests/{request_id}/timeline", response_model=TimelineOut,
           operation_id="getPayoutRequestTimeline", tags=["payouts"], responses=_E503)
    def payout_timeline(request_id: RequestId, owner: Owner, request: Request):
        desk = PayoutDesk(_platform(request))
        return desk.history(desk.owned(owner, request_id)).timeline(payout=True)

    @r.post("/payout-requests/{request_id}/approve", response_model=PayoutOut,
            operation_id="approvePayoutRequest", tags=["payouts"], responses=_E503)
    def approve_payout(request_id: RequestId, body: ApprovalDecision, owner: Owner, request: Request):
        return PayoutDesk(_platform(request)).decide(owner, request_id, body.terms_sha256)

    @r.post("/payout-requests/{request_id}/reject", response_model=PayoutOut,
            operation_id="rejectPayoutRequest", tags=["payouts"], responses=_E503)
    def reject_payout(request_id: RequestId, owner: Owner, request: Request):
        return PayoutDesk(_platform(request)).decide(owner, request_id, reject=True)

    @r.post("/payout-requests/{request_id}/execute", response_model=PayoutOut,
            operation_id="executePayoutRequest", tags=["payouts"], responses=_E503)
    def execute_payout(request_id: RequestId, agent: Agent, request: Request):
        return PayoutDesk(_platform(request)).execute(agent.ctx, request_id)

    @r.post("/payout-requests/{request_id}/reconciliation", response_model=PayoutOut,
            operation_id="reconcilePayoutRequest", tags=["payouts"], responses=_E503)
    def reconcile_payout(request_id: RequestId, caller: AnyCaller, request: Request):
        return PayoutDesk(_platform(request)).reconcile(caller, request_id)
