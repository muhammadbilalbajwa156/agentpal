"""Owner refund models; amounts are bounded exact decimal strings."""
from typing import Literal

from pydantic import Field

from .models import Amount, AmountOut, _In, _Out


class RefundCreate(_In):
    amount: Amount | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    reason: str | None = Field(default=None, max_length=255)


class RefundOut(_Out):
    refund_id: str
    request_id: str
    amount: AmountOut
    currency: str
    status: Literal['COMPLETED', 'PENDING', 'FAILED', 'CANCELLED', 'reconciliation_required']
    provider_refund_id: str | None
    failure_reason: Literal['merchant_permission_required', 'provider_rejected',
                            'paypal_unavailable'] | None = None
    created_at: str
    is_real_money: Literal[False] = False


class RefundList(_Out):
    request_id: str
    status: str
    captured_total: AmountOut
    refunded_total: AmountOut
    reserved_total: AmountOut
    refunds: list[RefundOut]
    is_real_money: Literal[False] = False


class DisputeOut(_Out):
    dispute_id: str
    request_id: str
    status: str
    reason: str | None = None


class DisputeList(_Out):
    disputes: list[DisputeOut]
    # Exactly one bounded provider page; no provider links or unrelated totals exposed.
    page_complete: Literal[False] = False
    is_real_money: Literal[False] = False
