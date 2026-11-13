import logging
from typing import Annotated

from fastapi import Header, HTTPException
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.orders._router import router
from app.core.clock import utc_now
from app.core.dependencies import AdminUser, DbSession, Provider
from app.core.errors import Problem, ProblemType
from app.core.idempotency import REFUND_KEY_HEADER_DESCRIPTION
from app.core.openapi import RETRY_AFTER_HEADER
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import PaymentDispute, PaymentRefund
from app.schemas.common import Int32
from app.schemas.order import (
    OrderPayment,
    OrderPaymentDispute,
    OrderPaymentDisputeEvidence,
    OrderPaymentList,
    OrderPaymentRefund,
    PaymentStatus,
    RefundCreate,
    RefundResponse,
)
from app.services.payment.attempts import (
    _cents_to_decimal,
    _decimal_to_cents,
    _provider_idempotency_key,
)
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)
from app.services.payment.order_state import (
    DISPUTED_CHARGE_CODES,
    _lock_order_and_reload,
)
from app.services.payment.refund_ledger import (
    _REFUND_STATUSES_NOT_RETURNED,
    _apply_refund_object,
    _has_open_dispute,
    _record_refund,
    _settle_refunds,
)

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


@router.post(
    "/{order_id}/refund",
    response_model=RefundResponse,
    responses={
        400: {"description": "The provider refused the refund as invalid (for example an amount above what is "
                             "left to refund; the detail then names the refundable amount), or the "
                             "Idempotency-Key header is missing or malformed"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        409: {"description": "Order not paid, no successful payment found, payment disputed, or idempotency key already used with different refund parameters"},
        503: {"description": "Payment provider unavailable or timed out (Retry-After: 5); retry with the same "
                             "Idempotency-Key",
              "headers": RETRY_AFTER_HEADER},
    },
)
async def refund_payment(
    order_id: Int32,
    refund_request: RefundCreate,
    current_user: AdminUser,  # Refunds are a support/admin action
    db: DbSession,
    provider: Provider,
    # Required like /pay's: without a key, a retried or double-clicked
    # partial refund was a second refund, the money returned twice.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description=REFUND_KEY_HEADER_DESCRIPTION,
    )],
):
    """Issue a full or partial refund for a paid order (admin only).

    Unlike /pay and /checkout-session, the key is not looked up locally
    first: a refund only gets its id from the provider. The Idempotency-Key
    header is forwarded to the payment provider (namespaced, see
    _provider_idempotency_key), which is where real Stripe dedupes refund
    requests by key - see StripePaymentProvider.create_refund's own
    idempotency cache, which mirrors that behavior for the mock. A retried
    refund request (client timeout, double-click "Refund") therefore gets
    the first one's refund back, and that refund is recorded once
    (PaymentRefund, by refund id).

    `amount` is optional: omit it for a full refund; it must be greater than
    zero and not more than what is left to refund on the payment. Only an
    order with payment_status paid can be refunded, and not while a dispute
    on its payment is open."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    if order.payment_status != PaymentStatus.PAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Only a paid order can be refunded"
        )

    # The charge that paid the order: its succeeded attempt that is not
    # flagged for manual review. An order charged twice (a stale attempt
    # completing after another paid it) has a second succeeded attempt,
    # flagged; that money is returned by whoever reconciles the flag. This
    # used to take the newest succeeded attempt whatever its flag, which
    # could be the second charge: the order was then marked refunded while
    # the charge that paid it was never returned, and could no longer be.
    payment_row = (await db.execute(
        select(Payment)
        .where(
            Payment.order_id == order.id,
            Payment.status == "succeeded",
            Payment.requires_manual_review.is_(False),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )).scalars().first()
    if not payment_row:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="No successful payment found for this order"
        )
    # Stripe: refunding a disputed charge returns the money twice - the
    # disputed amount has already been taken from the balance, and a lost
    # dispute is not offset by the refund.
    if await db.run_sync(_has_open_dispute, order.id, payment_row.id):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This payment is disputed; it cannot be refunded while the dispute is open",
        )

    refund_amount_cents = (
        _decimal_to_cents(refund_request.amount) if refund_request.amount is not None else None
    )

    # Nothing above wrote anything, but the two reads opened a transaction
    # and with it checked out a connection. End it before waiting on the
    # provider (see the note in process_payment): a pooled connection must not sit
    # idle in a transaction for the length of an external HTTP call.
    # expire_on_commit is off, so `order` and `payment_row` stay usable.
    payment_intent_id = payment_row.payment_intent_id
    await db.commit()

    try:
        refund = await run_in_threadpool(
            provider.create_refund,
            payment_intent_id=payment_intent_id,
            amount=refund_amount_cents,
            # Namespaced like /pay's (see _provider_idempotency_key).
            idempotency_key=_provider_idempotency_key("refund", order, idempotency_key),
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
        # The provider's wording is for developers (it names objects, ids and
        # amounts of its own): logged, and answered in ours.
        logger.warning(
            "Refund of order %s refused by the provider (code %s): %s",
            order.id, getattr(e, "code", None), e.message,
        )
        if getattr(e, "code", None) in DISPUTED_CHARGE_CODES:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="This payment is disputed; it cannot be refunded while the dispute is open",
            ) from e
        refundable = payment_row.amount - payment_row.amount_refunded
        if refund_amount_cents is not None and refund_amount_cents > refundable:
            # From our own records, not the provider's text.
            detail = (
                f"Refund amount exceeds the refundable amount of this payment "
                f"({_cents_to_decimal(max(refundable, 0))} {payment_row.currency})"
            )
        else:
            detail = "The refund could not be processed: the payment provider refused it"
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=detail) from e

    # The provider's word on this charge's other refunds, read before taking
    # the lock. A refund that failed at the provider after being recorded
    # still counts in amount_refunded until refund.failed is processed;
    # reconciling here avoids violating ck_payment_refunded_within_amount.
    try:
        provider_refunds = await run_in_threadpool(provider.list_refunds, payment_intent_id)
    except PaymentProviderError:
        provider_refunds = []

    # Re-read under the order's lock before adding to it: payment_row was
    # read before the provider call, and another refund of this charge may
    # have been recorded since. Adding to that stale copy overwrote it - two
    # refunds taken by the provider, one recorded, and an order whose
    # refunds covered the charge left "paid".
    await db.run_sync(_lock_order_and_reload, order, payment_row)

    def _sync_and_settle(db_session: Session) -> PaymentRefund:
        recorded = set(db_session.scalars(
            select(PaymentRefund.refund_id).where(PaymentRefund.payment_id == payment_row.id)
        ))
        for other in provider_refunds:
            # Only what this app counted can be overstated; a failed refund it
            # never recorded took nothing from amount_refunded.
            other_status = other.provider_status or other.status.value
            if (other.id != refund.id and other.id in recorded
                    and other_status in _REFUND_STATUSES_NOT_RETURNED):
                _apply_refund_object(db_session, payment_row, {
                    "id": other.id, "status": other_status, "amount": other.amount,
                })
        # Stripe's own status, not the folded one: a refund canceled at once
        # was recorded as pending and counted as money on its way back.
        row = _record_refund(
            db_session, payment_row, refund.id, refund.amount,
            refund.provider_status or refund.status.value, source="admin",
        )
        _settle_refunds(db_session, order, payment_row)
        if _has_open_dispute(db_session, order.id, payment_row.id):
            row.requires_manual_review = True
            logger.warning(
                "Payment %s has an open dispute after refund %s was issued; "
                "money has moved, flagging refund for manual review",
                payment_row.id, refund.id,
            )
        return row

    refund_row = await db.run_sync(_sync_and_settle)
    order.updated_at = utc_now()
    await db.commit()

    return RefundResponse(
        id=refund.id,
        order_id=order.id,
        amount=_cents_to_decimal(refund_row.amount),
        # The charge's own code, as on the order and the payment: Stripe
        # reports currencies in lower case and the live provider passes
        # Refund.currency through as sent, so this answered "usd" for a
        # USD order.
        currency=payment_row.currency,
        # As recorded: a replay, or a request whose refund a webhook has
        # already reported, says where the refund is now.
        status=refund_row.status,
        payment_status=order.payment_status,
    )


@router.get(
    "/{order_id}/payments",
    response_model=OrderPaymentList,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
    },
)
async def list_order_payments(
    order_id: Int32,
    current_user: AdminUser,
    db: DbSession,
):
    """Every payment attempt of an order, oldest first, with its refunds and
    disputes (admin only). Amounts are in major units, as everywhere in the
    API. `requires_manual_review` on an attempt (a second charge, a success
    for a cancelled order or for the wrong amount) or on a refund (one that
    failed after it was recorded) marks what a person has to reconcile; an
    open dispute (closed_at null) holds the order."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    payments = (await db.execute(
        select(Payment).where(Payment.order_id == order.id).order_by(Payment.id)
    )).scalars().all()
    payment_ids = [p.id for p in payments]
    refunds = (await db.execute(
        select(PaymentRefund).where(
            PaymentRefund.payment_id.in_(payment_ids)
        ).order_by(PaymentRefund.id)
    )).scalars().all()
    disputes = (await db.execute(
        select(PaymentDispute).where(
            PaymentDispute.payment_id.in_(payment_ids)
        ).order_by(PaymentDispute.id)
    )).scalars().all()
    return OrderPaymentList(items=[
        OrderPayment(
            id=p.id,
            status=p.status,
            amount=_cents_to_decimal(p.amount),
            amount_refunded=_cents_to_decimal(p.amount_refunded),
            currency=p.currency,
            transaction_id=p.payment_intent_id,
            checkout_session_id=p.checkout_session_id,
            requires_manual_review=p.review_open,
            created_at=p.created_at,
            refunds=[
                OrderPaymentRefund(
                    id=r.refund_id,
                    amount=_cents_to_decimal(r.amount),
                    currency=r.currency,
                    status=r.status,
                    source=r.source,
                    failure_reason=r.failure_reason,
                    requires_manual_review=r.requires_manual_review,
                    created_at=r.created_at,
                )
                for r in refunds if r.payment_id == p.id
            ],
            disputes=[
                OrderPaymentDispute(
                    id=d.dispute_id,
                    amount=_cents_to_decimal(d.amount),
                    currency=d.currency,
                    status=d.status,
                    reason=d.reason,
                    closed_at=d.closed_at,
                    created_at=d.created_at,
                    evidence=(
                        OrderPaymentDisputeEvidence(
                            due_by=d.evidence_due_by,
                            has_evidence=d.evidence_has_evidence,
                            past_due=d.evidence_past_due,
                            submission_count=d.evidence_submission_count,
                            submission_method=d.evidence_submission_method,
                        )
                        if any(value is not None for value in (
                            d.evidence_due_by, d.evidence_has_evidence, d.evidence_past_due,
                            d.evidence_submission_count, d.evidence_submission_method,
                        ))
                        else None
                    ),
                )
                for d in disputes if d.payment_id == p.id
            ],
        )
        for p in payments
    ])
