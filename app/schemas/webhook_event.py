from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WebhookEventResponse(BaseModel):
    """A stored Stripe event as an admin sees it. The payload is not part of
    it: it is the provider's raw object (customer details, amounts), and an
    admin needs the state, not the content, to decide on a replay."""

    id: int
    event_id: str
    event_type: str
    status: str
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
