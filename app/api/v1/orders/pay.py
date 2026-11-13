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
    _ensure_same_request_in_flow,
    _payment_by_key,
    _payment_row_to_response,
    _replay_payment,
)
from app.api.v1.orders._payments import (
    _guard_currency_matches_order,
    _guard_no_attempt_in_flight,
    _guard_order_payable,
    _guard_payment_window_open,
    _log_attempt_left_unknown,
)
from app.api.v1.orders._router import router
from app.core.clock import utc_now
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
    PaymentCreate,
    PaymentResponse,
    PaymentStatus,
)
from app.services.payment.attempts import (
    _PENDING_INTENT_ID_PREFIX,
    _decimal_to_cents,
    _is_checkout_attempt,
    _provider_idempotency_key,
)
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
)
from app.services.payment.order_state import (
    _apply_payment_failure,
    _apply_payment_success,
)
from app.services.payment.types import PaymentIntentStatus

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


@router.post(
    "/{order_id}/pay",
    response_model=PaymentResponse,
    responses={
        400: {"description": "Amount or currency does not match the order, the provider did not accept the "
                             "payment details (check the payment method and try again), or the "
                             "Idempotency-Key header is missing or malformed"},
        404: {"description": "Order not found (an order of another user is not found either)"},
        409: {
            "description": "Order already paid, refunded or cancelled; another attempt for this order is in progress; "
                           "the Idempotency-Key belongs to an attempt still being processed (Retry-After: 5), "
                           "or to a Checkout Session attempt that must be retried on /checkout-session; "
                           "the key was already used with different payment parameters; "
                           "the payment was taken but the order could not be completed (it was cancelled "
                           "or paid meanwhile; the payment is flagged for a refund); "
                           "or the order's payment window has closed (a new key on an order placed "
                           "more than UNPAID_ORDER_EXPIRE_MINUTES ago): place a new order",
            "headers": RETRY_AFTER_HEADER,
        },
        422: {"description": "Card declined or payment not completed (payment-declined; the order's payment_status "
                             "becomes failed and a new Idempotency-Key is needed), or the key was already used "
                             "with a different request body"},
        503: {"description": "Payment provider unavailable or timed out (Retry-After: 5); the attempt is left "
                             "unknown: retry with the same Idempotency-Key",
              "headers": RETRY_AFTER_HEADER},
    },
)
async def process_payment(
    order_id: Int32,
    payment: PaymentCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    provider: Provider,
    # Required, as on POST /orders and for the same reason. While it was
    # optional, two requests without it that arrived together (a double
    # click, a second tab, a retry after a lost response) both passed the
    # unpaid guard below and both charged the card: the order lock is not
    # held across the provider call, so the second success could only be
    # flagged for manual review, after the customer was charged twice. With
    # a key, the second request of one attempt is the first one's replay.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description=PAYMENT_KEY_HEADER_DESCRIPTION,
    )],
):
    """Charge an order's total via the payment provider, creating a PaymentIntent.

    Requires an `Idempotency-Key` header. Repeating a request with a key
    already used for this order returns the first attempt's outcome; a key
    already used with a different request body is answered 422.

    Body (changed in 6.0.0): `{"amount": "<order total>", "currency":
    "<optional, the order's>", "payment_method_token": "<pm_... from
    Stripe.js>"}`. `payment_method_token` is required; `order_id` (the order
    is the one in the path) and `payment_method` (always Stripe) are no
    longer accepted, and a body that still sends them is a 422
    (`extra_forbidden`). The response still carries `order_id` and
    `payment_method` ("stripe").

    Payment window: a new attempt (a new key, on this route or on
    /checkout-session) is accepted only within UNPAID_ORDER_EXPIRE_MINUTES
    (30 by default) of the order's `created_at`; after that it is a 409 and
    the unpaid order is cancelled by the expiry task. A retry with the key of
    an attempt started in time is still served. A card declined after the
    window cannot be retried with a new key: the customer places a new order.

    Only one attempt per order can be in progress: another one is a 409
    until the first settles, or PAYMENT_ATTEMPT_STALE_AFTER_MINUTES (5 by
    default) have passed. `amount` must equal the order's total, and
    `currency`, if sent, its currency. The 200 answer is not always a paid
    order: `status` is `paid`, `requires_action` (3D Secure: finish it with
    `client_secret` and `next_action`; the outcome then arrives by webhook)
    or `pending` (accepted, not settled yet). A declined attempt is a 422;
    repeating its key answers 200 with status `failed`. This route is limited per user by
    RATE_LIMIT_CHECKOUT (10/minute by default).
    """

    # Locked (SELECT ... FOR UPDATE) until the commit that records this
    # attempt below, so two attempts check the guards and record themselves
    # one after the other, never both against the same unpaid state. The
    # lock is not held across the provider call (no transaction stays open
    # while Stripe is waited on); what can still happen after it is handled
    # where the outcome is applied (_lock_order_and_reload).
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

    # A retried checkout-payment request (client timeout + retry, double
    # click on "Pay") with the same key returns the original attempt's
    # outcome instead of creating a second PaymentIntent / re-charging the
    # card. This is our own DB-level idempotency guard, independent of (and
    # in addition to) the provider's own idempotency-key cache below - a
    # real integration needs both, since a crash between the provider call
    # succeeding and our own commit would otherwise be invisible to us.
    # Checked *before* the "already paid" guard below: a successful retry
    # of the same key on an order that is now paid is exactly the case this
    # guard exists for, and must not be rejected as a duplicate payment.
    existing_payment = await _payment_by_key(db, order.id, idempotency_key)
    # What defines the request is the whole validated body (amount, currency,
    # token), so a retry that changes any of it is not a replay. The
    # order is fixed by the path and the key is scoped to it.
    fingerprint = request_fingerprint(
        operation="pay", body=payment.model_dump(mode="json")
    )
    _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=False)
    #
    # One exception: an attempt whose provider request timed out ("unknown")
    # is not an outcome to replay. Stripe's guidance for a request that may
    # or may not have been executed is to send it again with the same
    # idempotency key and parameters, and Stripe then returns the original
    # result; so the retry below goes through to the provider again with
    # this attempt's own keys. Answering it with a "failed" row sent the
    # client to a new key, which could create and charge a second
    # PaymentIntent for the same order.
    retried_attempt = None
    if existing_payment:
        is_stale_processing = (
            existing_payment.status == "processing"
            and existing_payment.created_at
            <= utc_now() - timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
        )
        if is_stale_processing:
            existing_payment.status = "unknown"
        if existing_payment.status != "unknown":
            return _replay_payment(existing_payment, order)
        if _is_checkout_attempt(existing_payment):
            # Sending it as a PaymentIntent would be a second way to
            # charge the order; only /checkout-session settles it.
            raise Problem(
                status.HTTP_409_CONFLICT,
                type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
                detail=(
                    "This Idempotency-Key belongs to a checkout session attempt "
                    "whose outcome is not known yet; retry it on "
                    "/checkout-session with the same Idempotency-Key"
                ),
            )
        retried_attempt = existing_payment

    await _check_request(db, order, payment, retried_attempt)

    amount_cents = _decimal_to_cents(payment.amount)
    provider_idempotency_key = _provider_idempotency_key("pay", order, idempotency_key)

    if retried_attempt is not None:
        # Claimed under the order's lock: a second retry of the same key
        # now finds the attempt "processing" and is answered with that,
        # instead of sending the provider the same request at the same time.
        payment_row = retried_attempt
        payment_row.status = "processing"
        await db.commit()
    else:
        payment_row = Payment(
            order_id=order.id,
            provider="stripe",
            payment_intent_id=f"{_PENDING_INTENT_ID_PREFIX}{uuid.uuid4().hex[:24]}",
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
            # The rollback expires `order`, which the replay below reads: it
            # is reloaded (an expired attribute cannot be read on an
            # AsyncSession).
            await db.rollback()
            await db.refresh(order)
            existing_payment = await _payment_by_key(db, order.id, idempotency_key)
            if existing_payment:
                _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=False)
                return _replay_payment(existing_payment, order)
            raise

    intent = await _charge(
        db, provider, order, payment_row, payment, amount_cents, provider_idempotency_key
    )

    return await _answer(
        db, order, payment_row, payment, intent, background_tasks, current_user.email
    )


async def _check_request(db, order, payment, retried_attempt) -> None:
    """Validate that the order and payment parameters accept a new or retried attempt."""
    _guard_order_payable(order)
    if retried_attempt is None:
        _guard_payment_window_open(order)
    await db.run_sync(
        _guard_no_attempt_in_flight, order,
        except_payment_id=retried_attempt.id if retried_attempt else None,
    )

    if payment.amount != order.total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment amount does not match order total"
        )
    _guard_currency_matches_order(payment.currency, order)


async def _charge(
    db, provider, order, payment_row, payment, amount_cents, provider_idempotency_key
):
    """Create and confirm the payment intent with the provider."""
    # Every provider call in this module goes through run_in_threadpool.
    # The provider is synchronous (the Stripe SDK makes blocking HTTP calls:
    # measured 0.4 s for a create and 0.7 s for a confirm against Stripe's
    # test mode), and these handlers run on the event loop, so a direct call
    # stopped the whole worker for the duration of every payment - no other
    # request was served for about a second. Each call is made with no
    # database transaction open (the commit just above it), so no pooled
    # connection is held while waiting on Stripe.
    try:
        intent = await run_in_threadpool(
            provider.create_payment_intent,
            amount=amount_cents,
            currency=order.currency,
            idempotency_key=provider_idempotency_key,
            payment_method=payment.payment_method_token,
            metadata={"order_id": str(order.id)},
        )
        payment_row.payment_intent_id = intent.id
        payment_row.client_secret = intent.client_secret
        await db.commit()

        # A different key from the create call above, derived from it.
        # Stripe scopes an idempotency key to one request: reusing the
        # create call's key on the confirm endpoint is rejected with
        # "Keys for idempotent requests can only be used with the same
        # parameters they were first used with". With one shared key, as
        # this used to be, every /pay against real Stripe failed with 409 -
        # found by an end-to-end run against Stripe's test mode
        # (docs/reports/load-testing-3x.md); the in-process mock accepted it,
        # so the test suite never saw it. The mock now rejects it too.
        #
        # return_url is where 3D Secure sends the customer back after
        # authenticating: the frontend's checkout page for this order, built
        # on FRONTEND_URL like the Checkout Session pages. Stripe adds the
        # PaymentIntent's id and client secret to it as query parameters.
        intent = await run_in_threadpool(
            provider.confirm_payment_intent,
            intent.id, idempotency_key=f"{provider_idempotency_key}:confirm",
            return_url=f"{settings.FRONTEND_URL}/checkout/return?order_id={order.id}",
        )
    except PaymentProviderTimeoutError as e:
        # Provider unreachable/timed out: we do not know whether Stripe
        # actually processed anything, so the order's payment_status/status
        # must stay exactly as they were before this attempt - only this
        # attempt's own Payment row is marked "unknown" (not failed: it may
        # have been carried out). It keeps its provider keys, so a retry
        # with the same Idempotency-Key is sent again and settles it, and
        # it blocks other attempts meanwhile (_guard_no_attempt_in_flight).
        # Regression test for: a transport failure silently leaving the
        # order half-updated.
        payment_row.status = "unknown"
        await db.commit()
        _log_attempt_left_unknown(e, "pay", payment_row, provider_idempotency_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    except IdempotencyError:
        await db.rollback()
        # Refused before it was carried out: still not settled. The
        # rollback expired the attempt's row; it is reloaded before it
        # is written. Fresh attempts as well as retried attempts must
        # be marked unknown so they do not get stuck as processing.
        await db.refresh(payment_row)
        payment_row.status = "unknown"
        await db.commit()
        raise Problem(
            status.HTTP_409_CONFLICT,
            type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
            detail="Idempotency key already used with different payment parameters",
        ) from None
    except InvalidRequestError as e:
        # A structurally invalid amount/currency (e.g. a non-integer or
        # non-positive amount, or a currency code that isn't a real ISO
        # 4217 code) - the request was never "sent to the processor", so
        # this attempt is a hard failure, not a retryable transport issue.
        payment_row.status = "failed"
        order.updated_at = utc_now()
        await db.commit()
        # The provider's wording ("No such PaymentMethod: 'pm_...'") is for
        # developers: logged, and answered in ours.
        logger.warning(
            "Payment %s of order %s refused by the provider as invalid (code %s): %s",
            payment_row.id, order.id, e.code, e.message,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The payment details were not accepted: check the payment method and try again",
        ) from e
    except CardError as e:
        # The provider *did* respond - with a decline. This is a terminal
        # outcome for this attempt (not a transport failure), so the order
        # is marked failed (not left pending) and the customer can retry
        # with a new attempt/new Idempotency-Key. Through the shared helper:
        # the order may have been paid by another attempt (a Checkout
        # Session completing) since the guard above read it.
        await db.run_sync(_apply_payment_failure, order, payment_row)
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.PAYMENT_DECLINED,
            detail=e.message,
        ) from e

    return intent


async def _answer(
    db, order, payment_row, payment, intent, background_tasks, user_email
) -> PaymentResponse:
    """Handle the PaymentIntent outcome and build the payment response."""
    if intent.status == PaymentIntentStatus.REQUIRES_ACTION:
        # 3D Secure / SCA required: a distinct, non-terminal state - must
        # not be conflated with either success or failure. The order is
        # left exactly as-is (still pending payment); the client uses
        # client_secret to complete authentication, after which either a
        # follow-up confirm or the payment_intent.succeeded webhook
        # finalizes it.
        payment_row.status = "requires_action"
        await db.commit()
        return PaymentResponse(
            order_id=order.id,
            payment_method="stripe",
            amount=payment.amount,
            currency=order.currency,
            payment_method_token=payment.payment_method_token,
            id=payment_row.id,
            status=PaymentStatus.REQUIRES_ACTION,
            created_at=payment_row.created_at,
            transaction_id=intent.id,
            client_secret=intent.client_secret,
            requires_action=True,
            next_action=intent.next_action,
        )

    if intent.status == PaymentIntentStatus.PROCESSING:
        # Accepted but not settled yet (Stripe's "processing", and any
        # status the live provider does not model, e.g. requires_capture,
        # which it reports as PROCESSING). Not a success: no money has been
        # taken. The attempt stays in flight and its outcome arrives by
        # webhook, as for 3D Secure above. This branch and the one below
        # used to be missing - everything other than requires_action was
        # applied as a success, and the order marked paid and confirmed.
        return _payment_row_to_response(payment_row, order)

    if intent.status != PaymentIntentStatus.SUCCEEDED:
        # Confirmed without an error and still unpaid (requires_payment_method,
        # canceled, ...): a failed attempt, like a decline.
        await db.run_sync(_apply_payment_failure, order, payment_row)
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.PAYMENT_DECLINED,
            detail="The payment was not completed; please try again or use another payment method",
        )

    applied = await db.run_sync(
        _apply_payment_success, order, payment_row, background_tasks, user_email
    )
    if not applied:
        raise Problem(
            status.HTTP_409_CONFLICT,
            detail=(
                "The payment was taken but the order could not be completed "
                "(cancelled or already paid); it has been flagged for a refund"
            ),
        )

    return PaymentResponse(
        order_id=order.id,
        payment_method="stripe",
        amount=payment.amount,
        currency=order.currency,
        payment_method_token=payment.payment_method_token,
        id=payment_row.id,
        status=PaymentStatus.PAID,
        created_at=payment_row.created_at,
        transaction_id=intent.id,
        client_secret=intent.client_secret,
    )
