from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.payment import WEBHOOK_EVENT_STATUSES
from app.schemas.common import ResponseBody, published_values


class WebhookEventResponse(ResponseBody):
    """A stored Stripe event as an admin sees it. The payload is not part of
    it: it is the provider's raw object (customer details, amounts), and an
    admin needs the state, not the content, to decide on a replay."""

    id: int
    event_id: str
    # Any event type the Stripe endpoint is set up to send is stored, so the
    # set is open: the ones the shop acts on are listed in the description.
    event_type: str = Field(
        description=(
            "Stripe's event type, as received. Applied by the shop: payment_intent.succeeded, "
            "payment_intent.payment_failed, payment_intent.canceled, checkout.session.completed, "
            "checkout.session.async_payment_succeeded, checkout.session.async_payment_failed, "
            "checkout.session.expired, refund.created, refund.updated, refund.failed, charge.refunded, "
            "charge.dispute.created, charge.dispute.updated, charge.dispute.closed, "
            "charge.dispute.funds_withdrawn, charge.dispute.funds_reinstated. "
            "Any other type is stored and marked processed without effect."
        )
    )
    status: Annotated[str, published_values(WEBHOOK_EVENT_STATUSES)] = Field(
        description=(
            "received: stored, not applied yet; processing: a worker holds it; processed: applied "
            "(or nothing to apply); retrying: an attempt failed, retried with a backoff; "
            "failed: out of retries, can be replayed."
        )
    )
    attempts: int
    last_error: str | None = None
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookEventListResponse(BaseModel):
    items: list[WebhookEventResponse]
    total: int
    page: int
    size: int
    has_more: bool
