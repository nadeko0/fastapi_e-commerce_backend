import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, Query
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from app.api.v1.admin._router import router
from app.api.v1.orders import (
    _REFUND_STATUSES_NOT_RETURNED,
    DISPUTED_CHARGE_CODES,
    REFUND_KEY_HEADER_DESCRIPTION,
    _cents_to_decimal,
    _has_open_dispute,
    _lock_order_and_reload,
    _provider_idempotency_key,
    _record_refund,
)
from app.core.clock import utc_now
from app.core.dependencies import AdminUser, DbSession, Provider
from app.core.errors import Problem, ProblemType
from app.core.openapi import (
    RETRY_AFTER_HEADER,
)
from app.core.sorting import order_clauses
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.admin import (
    AdminPaymentListResponse,
    AdminPaymentResponse,
)
from app.schemas.common import Int32, PaginationParams, sortable_pagination
from app.schemas.order import RefundResponse
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)

# The name the module had before the admin package split: log filters and alerts keep matching.
logger = logging.getLogger("app.api.v1.admin")

PAYMENT_SORT_COLUMNS = {
    "id": Payment.id,
    "created_at": Payment.created_at,
    "amount": Payment.amount,
}

if TYPE_CHECKING:
    AdminPaymentListParams = PaginationParams
else:
    AdminPaymentListParams = sortable_pagination(
        "AdminPaymentListParams", list(PAYMENT_SORT_COLUMNS)
    )


@router.get(
    "/payments",
    response_model=AdminPaymentListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid query parameters"},
    },
)
async def list_payments(
    pagination: Annotated[AdminPaymentListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    requires_manual_review: Annotated[bool | None, Query(
        description="true: only attempts flagged for manual review whose flag is still open; "
                    "false: only those without an open flag; omitted: all."
    )] = None,
    order_id: Annotated[Int32 | None, Query(description="Only the payment attempts of this order.")] = None,
):
    """List payment attempts across orders, optionally filtering by
    requires_manual_review or order_id, with sorting and pagination."""
    filters = []
    if requires_manual_review is not None:
        filters.append(Payment.review_open if requires_manual_review else ~Payment.review_open)
    if order_id is not None:
        filters.append(Payment.order_id == order_id)

    total = await db.scalar(select(func.count()).select_from(Payment).where(*filters))
    assert total is not None, "SELECT count(*) always returns an integer"
    payments = (
        await db.execute(
            select(Payment)
            .where(*filters)
            .order_by(
                *order_clauses(
                    pagination.sort_by,
                    pagination.sort_order,
                    PAYMENT_SORT_COLUMNS,
                    Payment.id,
                    [Payment.created_at.desc(), Payment.id.desc()],
                )
            )
            .offset((pagination.page - 1) * pagination.size)
            .limit(pagination.size)
        )
    ).scalars().all()

    return AdminPaymentListResponse(
        items=[
            AdminPaymentResponse.model_validate(p).model_copy(
                update={"requires_manual_review": p.review_open}
            )
            for p in payments
        ],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )


@router.post(
    "/payments/{payment_id}/refund",
    response_model=RefundResponse,
    responses={
        400: {"description": "The provider refused the refund (its reason is logged, not returned), or the "
                             "Idempotency-Key header is missing or malformed"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Payment or its order not found"},
        409: {"description": "Only a succeeded payment flagged for manual review can be refunded; the payment is "
                             "disputed; it has already been fully refunded (also when another refund of it was "
                             "taken at the same time); or the idempotency key was already used with different "
                             "refund parameters"},
        503: {"description": "Payment provider unavailable or timed out (Retry-After: 5); retry with the same "
                             "Idempotency-Key",
              "headers": RETRY_AFTER_HEADER},
    },
)
async def refund_flagged_payment(
    payment_id: Int32,
    current_user: AdminUser,
    db: DbSession,
    provider: Provider,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=255,
            description=REFUND_KEY_HEADER_DESCRIPTION,
        ),
    ],
):
    """Issue a refund of everything still refundable on a payment attempt
    flagged for manual review (a second charge, or a success for a cancelled
    order or for the wrong amount). Only a succeeded payment with an open
    flag can be refunded, and not while a dispute on it is open; there is no
    `amount`: it is always the remaining amount. Repeat a lost request with
    the same `Idempotency-Key`."""
    payment = (
        await db.execute(select(Payment).where(Payment.id == payment_id).limit(1))
    ).scalars().first()
    if not payment:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    order = (
        await db.execute(select(Order).where(Order.id == payment.order_id).limit(1))
    ).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    provider_key = _provider_idempotency_key("refund", order, idempotency_key)

    if not (payment.status == "succeeded" and payment.requires_manual_review):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Only a succeeded payment flagged for manual review can be refunded",
        )

    # Under dispute the money is already held back by the card network: a refund
    # would return it twice, and Stripe refuses it anyway.
    if await db.run_sync(_has_open_dispute, order.id, payment.id):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This payment is disputed; it cannot be refunded while the dispute is open",
        )

    # A retry of a refund that went through ends here, with no call to the
    # provider: nothing can move twice.
    remaining_cents = payment.amount - payment.amount_refunded
    if remaining_cents <= 0:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This payment has already been fully refunded",
        )

    payment_intent_id = payment.payment_intent_id
    await db.commit()

    try:
        refund = await run_in_threadpool(
            provider.create_refund,
            payment_intent_id=payment_intent_id,
            amount=remaining_cents,
            idempotency_key=provider_key,
        )
    except PaymentProviderTimeoutError as e:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    except IdempotencyError:
        raise Problem(
            http_status.HTTP_409_CONFLICT,
            type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
            detail="Idempotency key already used with different refund parameters",
        ) from None
    except (CardError, InvalidRequestError) as e:
        # The provider's wording is for developers: logged, and answered in
        # ours, as on POST /orders/{order_id}/refund.
        logger.warning(
            "Refund of payment %s (order %s) refused by the provider (code %s): %s",
            payment.id, order.id, getattr(e, "code", None), e.message,
        )
        if getattr(e, "code", None) in DISPUTED_CHARGE_CODES:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="This payment is disputed; it cannot be refunded while the dispute is open",
            ) from e
        # Two admins refunding the same flagged payment at once both pass the
        # guards above (nothing is locked across the provider call); the
        # provider takes the first refund and refuses the second, whose
        # winner may not be recorded here yet. The provider's own refunds
        # tell that conflict apart from a refusal of another kind.
        try:
            provider_refunds = await run_in_threadpool(provider.list_refunds, payment_intent_id)
        except PaymentProviderError:
            provider_refunds = []
        refunded_at_provider = sum(
            r.amount for r in provider_refunds
            if (r.provider_status or r.status.value) not in _REFUND_STATUSES_NOT_RETURNED
        )
        if refunded_at_provider >= payment.amount:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="This payment has already been fully refunded",
            ) from e
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="The refund could not be processed: the payment provider refused it",
        ) from e

    # Under the order's lock, like every refund event: a Dashboard refund's
    # webhook recorded at the same time would otherwise overwrite this one's
    # amount_refunded (or this one, it).
    await db.run_sync(_lock_order_and_reload, order, payment)
    refund_row = await db.run_sync(
        _record_refund,
        payment,
        refund.id,
        refund.amount,
        refund.provider_status or refund.status.value,
        source="admin",
    )
    payment.updated_at = utc_now()
    await db.commit()

    return RefundResponse(
        id=refund.id,
        order_id=order.id,
        amount=_cents_to_decimal(refund_row.amount),
        currency=payment.currency,
        status=refund_row.status,
        payment_status=order.payment_status,
    )
