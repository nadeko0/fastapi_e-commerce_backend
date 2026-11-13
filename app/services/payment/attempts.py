from decimal import Decimal

from app.models.order import Order
from app.models.payment import Payment


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
