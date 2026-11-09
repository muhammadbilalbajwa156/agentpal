"""P4 owner-only PayPal operations; registered by the existing /v1 scaffold."""
from datetime import datetime

from fastapi import APIRouter, Request

from .paypal_ops import PaypalReconciliation, reconcile


def add_ops_routes(r: APIRouter) -> None:
    from .router import Owner, _platform, _E503

    @r.get('/paypal/reconciliation', response_model=PaypalReconciliation,
           operation_id='reconcileWithPaypal', tags=['paypal'], responses=_E503)
    def reconcile_with_paypal(owner: Owner, request: Request, start: datetime, end: datetime):
        return reconcile(_platform(request), owner, start, end)
