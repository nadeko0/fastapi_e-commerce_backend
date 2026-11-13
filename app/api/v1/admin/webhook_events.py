from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from pydantic import Field
from sqlalchemy import func, select

from app.api.v1.admin._router import router
from app.core.dependencies import AdminUser, DbSession
from app.core.sorting import order_clauses
from app.models.payment import (
    WEBHOOK_EVENT_FAILED,
    WEBHOOK_EVENT_RECEIVED,
    WEBHOOK_EVENT_STATUSES,
    WebhookEvent,
)
from app.schemas.common import PaginationParams, sortable_pagination
from app.schemas.webhook_event import WebhookEventListResponse, WebhookEventResponse
from app.services.stripe_events import enqueue_event, mark_not_enqueued

WEBHOOK_EVENT_SORT_COLUMNS = {
    "id": WebhookEvent.id,
    "created_at": WebhookEvent.created_at,
    "status": WebhookEvent.status,
}
if TYPE_CHECKING:
    WebhookEventListParams = PaginationParams
else:
    WebhookEventListParams = sortable_pagination("WebhookEventListParams", tuple(WEBHOOK_EVENT_SORT_COLUMNS))

WebhookEventStatusFilter = Annotated[
    Annotated[str, Field(json_schema_extra={"enum": list(WEBHOOK_EVENT_STATUSES)})] | None,
    Query(description="Webhook event status."),
]


@router.get(
    "/webhook-events",
    response_model=WebhookEventListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid status value"},
    },
)
async def list_webhook_events(
    pagination: Annotated[WebhookEventListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    # Named "status" for the public API/query param; see list_orders above.
    status: WebhookEventStatusFilter = None,
):
    """
    List the Stripe webhook events the app stored, newest first, optionally
    by status (received, processing, processed, retrying, failed). Without
    the payload: it is the provider's raw object. A "failed" event ran out
    of retries and can be replayed.

    sort_by (id, created_at, status) and sort_order (asc, desc) sort the list, ties
    broken by id in the same direction; without sort_by, or with another value,
    the order is newest first.
    """
    filters = []
    if status:
        if status not in WEBHOOK_EVENT_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid webhook event status. Valid values are: {', '.join(WEBHOOK_EVENT_STATUSES)}",
            )
        filters.append(WebhookEvent.status == status)

    total = await db.scalar(select(func.count()).select_from(WebhookEvent).where(*filters))
    assert total is not None, "SELECT count(*) always returns an integer"
    events = (
        await db.execute(
            select(WebhookEvent)
            .where(*filters)
            .order_by(*order_clauses(
                pagination.sort_by, pagination.sort_order, WEBHOOK_EVENT_SORT_COLUMNS,
                WebhookEvent.id, [WebhookEvent.created_at.desc(), WebhookEvent.id.desc()],
            ))
            .offset((pagination.page - 1) * pagination.size)
            .limit(pagination.size)
        )
    ).scalars().all()
    return WebhookEventListResponse(
        items=[WebhookEventResponse.model_validate(event) for event in events],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )


@router.post(
    "/webhook-events/{event_id}/replay",
    response_model=WebhookEventResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Unknown webhook event"},
        409: {"description": "The event has not failed"},
    },
)
async def replay_webhook_event(
    event_id: str,
    current_user: AdminUser,
    db: DbSession,
):
    """
    Send a failed Stripe event through the worker again (after the cause of
    its failure is fixed): it becomes "received" with its attempts reset and
    is published. If the broker is down it stays received, marked, and the
    sweeper publishes it on its next run. Only a failed event can be replayed.
    """
    # Locked, so two admins replaying the same event publish it once: the
    # second finds it received.
    event = (
        await db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.event_id == event_id)
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if event is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Webhook event not found"
        )
    if event.status != WEBHOOK_EVENT_FAILED:
        # Read before the rollback: it expires the event, and an expired
        # attribute cannot be loaded on the loop.
        current_status = event.status
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Only a failed webhook event can be replayed (this one is {current_status})",
        )
    event.status = WEBHOOK_EVENT_RECEIVED
    event.attempts = 0
    await db.commit()
    if not await run_in_threadpool(enqueue_event, event.event_id):
        await db.run_sync(mark_not_enqueued, event.event_id)
    return WebhookEventResponse.model_validate(event)
