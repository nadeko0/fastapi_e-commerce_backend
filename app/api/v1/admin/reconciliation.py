import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1.admin._router import router
from app.core import database
from app.core.dependencies import AdminUser, DbSession, Provider
from app.core.sorting import order_clauses
from app.models.order import Order
from app.models.payment_discrepancy import PaymentDiscrepancy
from app.schemas.admin import (
    DiscrepancyListResponse,
    DiscrepancyResponse,
    ReconciliationQueued,
    ReconciliationRunResult,
)
from app.schemas.common import Int32, PaginationParams, sortable_pagination
from app.services.reconciliation import open_discrepancies, reconcile_payments
from app.tasks import check_payment_reconciliation

# The name the module had before the admin package split: log filters and alerts keep matching.
logger = logging.getLogger("app.api.v1.admin")

# -- Payment reconciliation ----------------------------------------------------------

DISCREPANCY_SORT_COLUMNS = {
    "id": PaymentDiscrepancy.id,
    "detected_at": PaymentDiscrepancy.detected_at,
    "last_seen_at": PaymentDiscrepancy.last_seen_at,
    "kind": PaymentDiscrepancy.kind,
}

if TYPE_CHECKING:
    DiscrepancyListParams = PaginationParams
else:
    DiscrepancyListParams = sortable_pagination("DiscrepancyListParams", tuple(DISCREPANCY_SORT_COLUMNS))


@router.get(
    "/reconciliation/discrepancies",
    response_model=DiscrepancyListResponse,
    responses={403: {"description": "Admin privileges required"}},
)
async def list_reconciliation_discrepancies(
    pagination: Annotated[DiscrepancyListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    order_id: Annotated[Int32 | None, Query(description="Only the discrepancies of this order.")] = None,
):
    """The differences the payment reconciliation found between our payments
    and the provider's and that still stand, oldest first (admin only):
    which payment, what kind of difference, what each side says, and since
    when. Optionally those of one order.

    sort_by (id, detected_at, last_seen_at, kind) and sort_order (asc, desc) sort
    the list, ties broken by id in the same direction; without sort_by, or with
    another value, the order is oldest first.
    """
    rows, total = await db.run_sync(
        open_discrepancies, page=pagination.page, size=pagination.size, order_id=order_id,
        order_by=order_clauses(
            pagination.sort_by, pagination.sort_order, DISCREPANCY_SORT_COLUMNS,
            PaymentDiscrepancy.id, [PaymentDiscrepancy.detected_at, PaymentDiscrepancy.id],
        ),
    )
    return DiscrepancyListResponse(
        items=[
            DiscrepancyResponse(
                id=d.id,
                order_id=p.order_id,
                payment_id=p.id,
                transaction_id=p.payment_intent_id,
                kind=d.kind,
                subject=d.subject,
                ours=d.ours,
                theirs=d.theirs,
                detected_at=d.detected_at,
                last_seen_at=d.last_seen_at,
            )
            for d, p in rows
        ],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )


@router.post(
    "/reconciliation/run",
    response_model=ReconciliationRunResult,
    responses={
        202: {"description": "The full run is queued (no order_id)", "model": ReconciliationQueued},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        503: {"description": "Task queue unavailable (Retry-After: 5), the full run was not queued"},
    },
)
async def run_reconciliation(
    current_user: AdminUser,
    db: DbSession,
    provider: Provider,
    order_id: Annotated[Int32 | None, Query(
        description="Reconcile only this order's payments, now; omit to queue the full run."
    )] = None,
):
    """Reconcile now (admin only). With order_id: every payment of that order,
    whatever its age, and the outcome in the response. Without: the same run
    as the daily one, queued for the worker (202), because it makes up to
    two provider calls per payment."""
    if order_id is None:
        try:
            # retry=False: with the broker down, fail at once instead of
            # sitting in kombu's publish retries.
            await run_in_threadpool(check_payment_reconciliation.apply_async, retry=False)
        except Exception as e:
            logger.warning("Could not queue the reconciliation run", exc_info=e)
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Task queue unavailable; reconciliation was not queued",
                headers={"Retry-After": "5"},
            ) from e
        return JSONResponse(
            ReconciliationQueued(queued=True).model_dump(),
            status_code=http_status.HTTP_202_ACCEPTED,
        )
    if not (await db.execute(select(Order.id).where(Order.id == order_id).limit(1))).first():
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )
    # End the read transaction: nothing stays open on the route's session
    # while the provider is called, and the thread works on a session of its
    # own (an AsyncSession must not be handed to another thread).
    await db.commit()

    def reconcile_order():
        with database.session_scope() as thread_db:
            return reconcile_payments(thread_db, provider, order_id=order_id).as_dict()

    return await run_in_threadpool(reconcile_order)
