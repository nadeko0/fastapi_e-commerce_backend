from decimal import Decimal

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Problem, ProblemType
from app.core.idempotency import ensure_same_request
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.order import (
    CheckoutSessionResponse,
    PaymentResponse,
    PaymentStatus,
)


async def _payment_by_key(
    db: AsyncSession, order_id: int, idempotency_key: str
) -> Payment | None:
    """The order's payment attempt recorded under this Idempotency-Key."""
    return (await db.execute(
        select(Payment).where(
            Payment.order_id == order_id,
            Payment.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


def _provider_idempotency_key(kind: str, order: Order, client_key: str) -> str:
    """
    The idempotency key sent to the payment provider for one request about
    `order`. A client's Idempotency-Key is unique only per order in our own
    tables (Payment: order_id + key), while the provider scopes keys to the
    whole account: sent as-is, the same key used for a second order of the
    same amount got the first order's PaymentIntent back, whose confirm
    then reported "succeeded" - the second order marked paid without being
    charged. So the key is namespaced by the kind of request and the order.
    """
    return f"{kind}-order-{order.id}:{client_key}"


def _decimal_to_cents(amount: Decimal) -> int:
    """Stripe (and every real payment processor) represents amounts as
    integers in the smallest currency unit - never as Decimal/float dollars.
    Converting once, here, at the API boundary defends against the classic
    cents-vs-dollars mistake propagating into the provider layer."""
    return int((amount * 100).to_integral_value())


def _cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal('0.01'))


# Payment.status (free-text column, mirrors the provider's own PaymentIntent
# status names) -> the more limited PaymentStatus enum exposed in the API.
_PAYMENT_ROW_STATUS_TO_API_STATUS = {
    "processing": PaymentStatus.PENDING,
    "unknown": PaymentStatus.PENDING,
    "requires_action": PaymentStatus.REQUIRES_ACTION,
    "succeeded": PaymentStatus.PAID,
    "failed": PaymentStatus.FAILED,
}


def _payment_row_to_response(payment_row: Payment, order: Order) -> PaymentResponse:
    return PaymentResponse(
        order_id=order.id,
        payment_method="stripe",
        amount=_cents_to_decimal(payment_row.amount),
        currency=payment_row.currency,
        id=payment_row.id,
        status=_PAYMENT_ROW_STATUS_TO_API_STATUS.get(payment_row.status, PaymentStatus.PENDING),
        created_at=payment_row.created_at,
        transaction_id=payment_row.payment_intent_id,
        client_secret=payment_row.client_secret,
        requires_action=payment_row.status == "requires_action",
    )


def _checkout_session_row_to_response(payment_row: Payment, order: Order) -> CheckoutSessionResponse:
    return CheckoutSessionResponse(
        order_id=order.id,
        payment_id=payment_row.id,
        checkout_session_id=payment_row.checkout_session_id,
        url=payment_row.checkout_session_url or "",
        status=_PAYMENT_ROW_STATUS_TO_API_STATUS.get(
            payment_row.status, PaymentStatus.PENDING
        ).value,
        amount=_cents_to_decimal(payment_row.amount),
        currency=payment_row.currency,
        created_at=payment_row.created_at,
    )


# Payment.payment_intent_id of an attempt until the provider has given it
# a real id (the column is NOT NULL and unique): /pay stores the
# PaymentIntent's id when it is created, and a Checkout Session attempt
# when its session completes.
_PENDING_INTENT_ID_PREFIX = "pending-"
# The placeholder of a Checkout Session attempt (still starts with the
# prefix above, so everything that looks for "pending-" sees it). It tells
# the two flows apart while an attempt has no session id: a key whose
# attempt timed out can only be retried by the flow that made it.
_PENDING_CHECKOUT_ID_PREFIX = f"{_PENDING_INTENT_ID_PREFIX}cs-"


def _is_checkout_attempt(payment_row: Payment) -> bool:
    """A Checkout Session attempt that has not stored a session id."""
    return payment_row.payment_intent_id.startswith(_PENDING_CHECKOUT_ID_PREFIX)


def _is_checkout_row(payment_row: Payment) -> bool:
    """An attempt of the Checkout Session flow, with or without its session."""
    return payment_row.checkout_session_id is not None or _is_checkout_attempt(payment_row)


def _ensure_same_request_in_flow(
    payment_row: Payment | None, fingerprint: str, *, checkout: bool
) -> None:
    """
    422 when the attempt recorded under the key came from this flow (/pay, or
    /checkout-session when `checkout`) with a different request. An attempt of
    the other flow keeps the answer it always had (a 409, or the replay): the
    two flows' fingerprints are not comparable.
    """
    if payment_row is not None and _is_checkout_row(payment_row) == checkout:
        ensure_same_request(payment_row.idempotency_fingerprint, fingerprint)


# What a request is told to wait (Retry-After, seconds) when its
# Idempotency-Key belongs to an attempt that is still being processed; the
# same wait a provider timeout asks for.
_ATTEMPT_RETRY_AFTER = 5


def _replay_payment(payment_row: Payment, order: Order) -> PaymentResponse:
    """
    /pay's answer to a key this order has already used with the same
    request: the outcome the first request ended with. An attempt still
    "processing" has none yet - its request is waiting on the provider, or
    the provider has not settled the payment (the webhook then does) - and
    the draft answers a request that repeats one still being processed with
    409 (draft-ietf-httpapi-idempotency-key-header, section 2.7), with a
    Retry-After so the client asks again. Replayed after it settled, the
    stored result is returned, as before.
    """
    if payment_row.status == "processing":
        raise Problem(
            status.HTTP_409_CONFLICT,
            type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
            detail=(
                "A request with this Idempotency-Key is still being processed; "
                "repeat it later with the same key"
            ),
            retry_after=_ATTEMPT_RETRY_AFTER,
        )
    return _payment_row_to_response(payment_row, order)


def _replay_checkout_session(payment_row: Payment, order: Order):
    """
    /checkout-session's answer to a key this order has already used. An
    attempt with a session is replayed as it is. One without a session - a
    twin request still waiting on the provider, a session the provider
    refused, a /pay attempt that used the same key - has nothing a
    CheckoutSessionResponse could describe, and the key cannot be used for
    a second attempt: the unique index on (order, key) refuses the row.
    Such a retry used to go on and hit that index, answering 500; it is a
    409 (the draft's answer to a request still being processed, section
    2.7, for the twin that is: it carries a Retry-After) telling the client
    to retry later, or to start a new attempt with a new key.
    """
    if payment_row.checkout_session_id:
        return _checkout_session_row_to_response(payment_row, order)
    raise Problem(
        status.HTTP_409_CONFLICT,
        type=ProblemType.IDEMPOTENCY_KEY_IN_USE,
        detail=(
            "This Idempotency-Key belongs to a payment attempt without a checkout "
            "session (still in progress, or failed); retry later, or start a new "
            "attempt with a new Idempotency-Key"
        ),
        retry_after=_ATTEMPT_RETRY_AFTER if payment_row.status == "processing" else None,
    )
