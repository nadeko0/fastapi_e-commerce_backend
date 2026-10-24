from datetime import datetime

from pydantic import BaseModel

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


class DiscrepancyResponse(BaseModel):
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


class ReconciliationQueued(BaseModel):
    """The full reconciliation run is queued for the worker (202)."""

    queued: bool
