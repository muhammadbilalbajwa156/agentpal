"""Owner-only PayPal refund and dispute routes (P2)."""
import re

from fastapi import APIRouter, Request, Response

from .errors import PlatformError
from .models import IDEMPOTENCY_KEY_PATTERN, contains_credential
from .refund_models import DisputeList, RefundCreate, RefundList, RefundOut
from .refunds import RefundDesk


def add_refund_routes(r: APIRouter) -> None:
    from .router import Owner, RequestId, _E503, _platform

    @r.post('/payment-requests/{request_id}/refunds', response_model=RefundOut,
            operation_id='refundPayment', status_code=201, tags=['refunds'], responses=_E503,
            openapi_extra={'parameters': [{'name': 'Idempotency-Key', 'in': 'header',
                           'required': True, 'schema': {'type': 'string', 'pattern': IDEMPOTENCY_KEY_PATTERN}}]})
    def refund_payment(request_id: RequestId, owner: Owner, body: RefundCreate,
                       request: Request, response: Response):
        key = request.headers.get('Idempotency-Key')
        if key is None:
            raise PlatformError(400, 'idempotency_key_required')
        if not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key) or contains_credential(key):
            raise PlatformError(422, 'invalid_request')
        result, replay = RefundDesk(_platform(request)).create(owner, request_id, body, key)
        if replay:
            response.status_code = 200
            response.headers['Idempotent-Replayed'] = 'true'
        return result

    @r.get('/payment-requests/{request_id}/refunds', response_model=RefundList,
           operation_id='listPaymentRefunds', tags=['refunds'], responses=_E503)
    def list_refunds(request_id: RequestId, owner: Owner, request: Request):
        return RefundDesk(_platform(request)).list(owner, request_id)

    @r.post('/payment-requests/{request_id}/refunds/reconciliation', response_model=RefundList,
            operation_id='reconcilePaymentRefunds', tags=['refunds'], responses=_E503)
    def reconcile_refunds(request_id: RequestId, owner: Owner, request: Request):
        return RefundDesk(_platform(request)).reconcile(owner, request_id)

    @r.get('/disputes', response_model=DisputeList, operation_id='listDisputes',
           tags=['disputes'], responses=_E503)
    def list_disputes(owner: Owner, request: Request):
        return RefundDesk(_platform(request)).disputes(owner)
