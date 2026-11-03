from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.payment import PAYMENT_STATUSES
from app.schemas.common import ResponseBody, published_values
from app.schemas.order import OutputMoney


class PopularProduct(BaseModel):
    id: int
    name: str
    total_quantity: int
    total_revenue: OutputMoney
    currency: str


class OrderStatistics(BaseModel):
    """GET /admin/stats: paid, not cancelled orders of the period."""

    period: str
    currency: str
    order_count: int
    total_revenue: OutputMoney
    average_order_value: OutputMoney
    popular_products: list[PopularProduct]


class DiscrepancyResponse(ResponseBody):
    """One difference the payment reconciliation found and that still stands."""

    id: int
    order_id: int
    payment_id: int
    transaction_id: str | None = None
    kind: str
    subject: str
    ours: str | None = None
    theirs: str | None = None
    detected_at: datetime
    last_seen_at: datetime


class DiscrepancyListResponse(BaseModel):
    items: list[DiscrepancyResponse]
    total: int
    page: int
    size: int
    has_more: bool


class ReconciliationRunResult(BaseModel):
    """The outcome of reconciling the payments of one order."""

    checked: int
    skipped: int
    errors: int
    opened: int
    resolved: int
    truncated: bool
    aborted: bool


class AdminPaymentResponse(ResponseBody):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    provider: Annotated[str, published_values(["stripe"])]
    payment_intent_id: str
    # An integer, unlike the decimal strings of OrderPayment and the other
    # money fields: this is the row as stored. Changing the type would change
    # the API, so it is stated here instead.
    amount: int = Field(
        description=(
            "In minor units (cents for USD/EUR; the smallest unit of the currency), as an integer. "
            "Unlike the decimal-string amounts of the other responses, e.g. 1050 is 10.50."
        )
    )
    amount_refunded: int = Field(
        description="Total refunded so far, in minor units (cents), as an integer; see amount."
    )
    currency: str
    status: Annotated[str, published_values(PAYMENT_STATUSES)] = Field(
        description=(
            "processing: the provider is being asked; unknown: it did not answer in time; "
            "requires_action: 3D Secure pending; succeeded; failed (declined, canceled or expired)."
        )
    )
    checkout_session_id: str | None = None
    requires_manual_review: bool
    created_at: datetime
    updated_at: datetime


class AdminPaymentListResponse(BaseModel):
    items: list[AdminPaymentResponse]
    total: int
    page: int
    size: int
    has_more: bool


class ReconciliationQueued(BaseModel):
    """The full reconciliation run is queued for the worker (202)."""

    queued: bool
