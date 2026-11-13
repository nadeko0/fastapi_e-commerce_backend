import logging
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Header,
    HTTPException,
    status,
)
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.v1.orders._payment_helpers import (
    _PENDING_CHECKOUT_ID_PREFIX,
    _checkout_session_row_to_response,
    _decimal_to_cents,
    _ensure_same_request_in_flow,
    _is_checkout_attempt,
    _payment_by_key,
    _provider_idempotency_key,
    _replay_checkout_session,
)
from app.api.v1.orders._payments import (
    _guard_currency_matches_order,
    _guard_no_attempt_in_flight,
    _guard_order_payable,
    _guard_payment_window_open,
    _log_attempt_left_unknown,
)
from app.api.v1.orders._router import router
from app.core.clock import as_utc, utc_now
from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, Provider
from app.core.errors import Problem, ProblemType
from app.core.idempotency import (
    PAYMENT_KEY_HEADER_DESCRIPTION,
    request_fingerprint,
)
from app.core.openapi import RETRY_AFTER_HEADER
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.common import Int32
from app.schemas.order import (
    CheckoutSessionCreate,
    CheckoutSessionResponse,
)
from app.services.payment.exceptions import (
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
)

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


@router.post(
    "/{order_id}/checkout-session",
    response_model=CheckoutSessionResponse,
    responses={
        400: {"description": "Currency does not match the order, the provider did not accept the payment "
                             "details, or the Idempotency-Key header is missing or malformed"},
        404: {"description": "Order not found (an order of another user is not found either)"},
        409: {
            "description": "Order already paid, refunded or cancelled; another attempt for this order is in progress; "
                           "the Idempotency-Key belongs to an attempt still being processed (Retry-After: 5), "
                           "or to a /pay attempt or one without a checkout session; "
                           "the key was already used with different checkout session parameters; "
                           "or the order's payment window has closed (a new key on an order placed "
                           "more than UNPAID_ORDER_EXPIRE_MINUTES ago): place a new order",
            "headers": RETRY_AFTER_HEADER,
        },
        422: {"description": "The key was already used with a different request body, or success_url or "
                             "cancel_url is not a plain URL on the frontend origin (FRONTEND_URL)"},
        503: {"description": "Payment provider unavailable or timed out (Retry-After: 5); the attempt is left "
                             "unknown: retry with the same Idempotency-Key",
              "headers": RETRY_AFTER_HEADER},
    },
)
async def create_checkout_session(
    order_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    provider: Provider,
    # Required like /pay's: two sessions created for one order without a
    # key can both be completed, and both charge the customer.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description=PAYMENT_KEY_HEADER_DESCRIPTION,
    )],
    payload: CheckoutSessionCreate | None = None,
):
    """
    Create a Stripe Checkout Session (the fully-hosted-redirect flow) for an
    order's total. Unlike /pay - which confirms a PaymentIntent server-side
    using a payment_method_token the client already collected via
    Stripe.js/Elements - this flow hands card entry/3DS/the entire payment
    UI to Stripe's own hosted page: the caller redirects the customer to the
    returned `url`, and the eventual outcome is reported back asynchronously
    via /webhooks/stripe (checkout.session.completed/.expired/
    .async_payment_failed), never in this response. Both flows remain
    available side by side; this does not replace or change /pay.

    Payment window: as on /pay, a new attempt is accepted only within
    UNPAID_ORDER_EXPIRE_MINUTES (30 by default) of the order's `created_at`;
    after that it is a 409 and the customer places a new order. A retry with
    the key of a session started in time returns that session.

    The session expires CHECKOUT_SESSION_EXPIRE_MINUTES (60 by default)
    after the attempt was created, and until then the customer cannot cancel
    the order (409). While an attempt is open, a new attempt with another key
    is a 409 for up to PAYMENT_ATTEMPT_STALE_AFTER_MINUTES (5 by default).
    The amount is always the order's total. This route is limited per user by
    RATE_LIMIT_CHECKOUT (10/minute by default).
    """
    payload = payload or CheckoutSessionCreate()

    # Locked like /pay's read of the order, for the same reason.
    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == current_user.id,
        ).with_for_update().limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    # Same idempotent-retry guard as /pay: a retried checkout-session
    # request (client timeout + retry, double click) with the same key
    # returns the original attempt's session instead of creating a second
    # one. Checked before the paid/cancelled guard below for the same
    # reason as /pay - a successful retry against an order that is now
    # paid must not be rejected as a fresh duplicate attempt.
    existing_payment = await _payment_by_key(db, order.id, idempotency_key)
    # The validated body is the request (the amount comes from the order).
    fingerprint = request_fingerprint(
        operation="checkout_session", body=payload.model_dump(mode="json")
    )
    _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=True)
    # Except an attempt of this flow whose provider request timed out
    # ("unknown"), as in /pay: Stripe's guidance for a request that may or
    # may not have been executed is to send it again with the same
    # idempotency key and parameters, and Stripe returns the original
    # session. A 409 that sent the client to a new key could create a
    # second Checkout Session for the order.
    retried_attempt = None
    if existing_payment:
        # As in /pay: a request that died between recording its attempt and
        # hearing from the provider left it "processing" with no session, and
        # every retry of its key was a 409 for ever. Past the stale window it
        # is "unknown" and sent again.
        if (_is_checkout_attempt(existing_payment)
                and existing_payment.status == "processing"
                and not existing_payment.checkout_session_id
                and existing_payment.created_at <= utc_now() - timedelta(
                    minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)):
            existing_payment.status = "unknown"
        if existing_payment.status != "unknown" or not _is_checkout_attempt(existing_payment):
            return _replay_checkout_session(existing_payment, order)
        retried_attempt = existing_payment

    _guard_order_payable(order)
    if retried_attempt is None:
        _guard_payment_window_open(order)
    await db.run_sync(
        _guard_no_attempt_in_flight, order,
        except_payment_id=retried_attempt.id if retried_attempt else None,
    )
    _guard_currency_matches_order(payload.currency, order)

    amount_cents = _decimal_to_cents(order.total_amount)
    provider_idempotency_key = _provider_idempotency_key("checkout", order, idempotency_key)
    # A client's own URLs are already known to be on the frontend's origin
    # (CheckoutSessionCreate); omitted ones are the frontend's checkout
    # pages. {CHECKOUT_SESSION_ID} is filled in by Stripe.
    success_url = payload.success_url or (
        f"{settings.FRONTEND_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = payload.cancel_url or f"{settings.FRONTEND_URL}/checkout/cancel"

    if retried_attempt is not None:
        # Claimed under the order's lock, as in /pay: a second retry of the
        # same key finds the attempt "processing" and is answered with that.
        payment_row = retried_attempt
        payment_row.status = "processing"
        await db.commit()
    else:
        payment_row = Payment(
            order_id=order.id,
            provider="stripe",
            payment_intent_id=f"{_PENDING_CHECKOUT_ID_PREFIX}{uuid.uuid4().hex[:24]}",
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            amount=amount_cents,
            currency=order.currency,
            status="processing",
        )
        db.add(payment_row)
        try:
            await db.commit()
        except IntegrityError:
            # Two concurrent requests with the same idempotency key both passed
            # the pre-check above; the unique index catches the duplicate here.
            # The rollback expires `order`, which the replay reads: reloaded
            # first (an expired attribute cannot be read on an AsyncSession).
            await db.rollback()
            await db.refresh(order)
            existing_payment = await _payment_by_key(db, order.id, idempotency_key)
            if existing_payment:
                _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=True)
                return _replay_checkout_session(existing_payment, order)
            # See the same re-raise in create_order: only the key's unique
            # index can fail this INSERT.
            raise  # pragma: no cover

    try:
        session = await run_in_threadpool(
            provider.create_checkout_session,
            amount=amount_cents,
            currency=order.currency,
            idempotency_key=provider_idempotency_key,
            success_url=success_url,
            cancel_url=cancel_url,
            description=f"Order #{order.id}",
            # payment_id names this attempt: the live provider copies the
            # metadata onto the session's PaymentIntent too, so a
            # payment_intent.* event can find its attempt before the session's
            # own event has stored the intent's id (stripe_events.apply_event).
            metadata={"order_id": str(order.id), "payment_id": str(payment_row.id)},
            expires_at=int(as_utc(payment_row.created_at).timestamp())
            + settings.CHECKOUT_SESSION_EXPIRE_MINUTES * 60,
        )
    except PaymentProviderTimeoutError as e:
        # Same reasoning as /pay: unknown whether Stripe actually created
        # the session, so only this attempt's own Payment row is touched,
        # marked "unknown" (not failed: it may have been carried out) - the
        # order's payment_status/status stay untouched. The row keeps its
        # key, so a retry with the same Idempotency-Key is sent again and
        # settles it, and it blocks other attempts meanwhile.
        payment_row.status = "unknown"
        await db.commit()
        _log_attempt_left_unknown(e, "checkout_session", payment_row, provider_idempotency_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    except IdempotencyError:
        await db.rollback()
        if retried_attempt is not None:
            # Refused before it was carried out: still not settled. The
            # rollback expired the attempt's row; reloaded before written.
            await db.refresh(payment_row)
            payment_row.status = "unknown"
            await db.commit()
        raise Problem(
            status.HTTP_409_CONFLICT,
            type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
            detail="Idempotency key already used with different checkout session parameters",
        ) from None
    except InvalidRequestError as e:
        payment_row.status = "failed"
        order.updated_at = utc_now()
        await db.commit()
        logger.warning(
            "Checkout session for payment %s of order %s refused by the provider as invalid "
            "(code %s): %s",
            payment_row.id, order.id, e.code, e.message,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The checkout session could not be created: the payment details were not accepted",
        ) from e

    payment_row.checkout_session_id = session.id
    payment_row.checkout_session_url = session.url
    await db.commit()

    return _checkout_session_row_to_response(payment_row, order)
