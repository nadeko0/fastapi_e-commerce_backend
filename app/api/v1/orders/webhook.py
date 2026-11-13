import json
import logging

from fastapi import (
    HTTPException,
    Request,
    status,
)
from fastapi.concurrency import run_in_threadpool

from app.api.v1.orders._router import router
from app.core.config import settings
from app.core.dependencies import DbSession, Provider
from app.schemas.order import StripeWebhookAck
from app.services.payment.exceptions import SignatureVerificationError

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


# Stripe-Signature is documented as required: a request without it is
# refused. It is read from the request, not declared as a Header parameter,
# because a required parameter would make FastAPI answer a missing header
# with its own 422; the refusal stays the 400 below, like a bad signature.
@router.post(
    "/webhooks/stripe",
    response_model=StripeWebhookAck,
    responses={400: {"description": "Missing or invalid webhook signature"}},
    openapi_extra={
        "parameters": [{
            "name": "Stripe-Signature",
            "in": "header",
            "required": True,
            "description": (
                "Stripe's signature of the payload, set by Stripe: t=<unix timestamp>,v1=<hex HMAC-SHA256 "
                "of \"<timestamp>.<raw request body>\" with the endpoint's signing secret>. A missing, "
                "wrong or too old signature is answered 400 "
                "(tolerance STRIPE_WEBHOOK_TOLERANCE_SECONDS). Only Stripe is expected to call this route."
            ),
            "schema": {"type": "string", "examples": ["t=1700000000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd"]},
        }]
    },
)
async def stripe_webhook(
    request: Request,
    db: DbSession,
    provider: Provider,
):
    """
    Receive Stripe webhook events for both payment flows. The event is
    verified, stored and acknowledged (200) here; the process_stripe_event
    task then reconciles order/payment state:
      - payment_intent.succeeded / payment_intent.payment_failed /
        payment_intent.canceled - the server-confirmed-PaymentIntent flow
        (/pay).
      - checkout.session.completed / .async_payment_succeeded /
        .async_payment_failed / .expired - the hosted-redirect Checkout
        Session flow (/checkout-session).
    Idempotent - duplicate deliveries of the same event.id are answered
    "already_processed" without being stored or applied again.

    No auth dependency: Stripe webhooks are unauthenticated by design and
    are instead trusted via the HMAC signature on the payload (verified
    below) - this is the correct, current Stripe-recommended pattern, not a
    missing-auth bug.
    """
    payload = await request.body()
    stripe_signature = request.headers.get("Stripe-Signature")

    try:
        event = provider.construct_webhook_event(
            payload,
            stripe_signature,
            settings.STRIPE_WEBHOOK_SECRET.get_secret_value(),
            tolerance_seconds=settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS,
        )
    except SignatureVerificationError:
        # Reject outright - never trust an unsigned/mis-signed payload,
        # and never let it touch any order/payment state.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        ) from None

    # Stripe's own advice (https://docs.stripe.com/webhooks): answer 2xx
    # quickly, before any complex logic, and handle events asynchronously.
    # The event is stored (with its payload), a Celery task is published to
    # apply it, and the answer goes out; app/services/stripe_events.py has
    # what an event does and how a failing one is retried.
    #
    # Stripe guarantees at-least-once delivery and retries unacknowledged
    # webhooks for up to 72 hours, so the same event.id can and will arrive
    # more than once. The unique constraint on event_id is the idempotency
    # guard: a duplicate is answered without being stored, published or
    # applied again. A store that fails raises, so the answer is a 5xx and
    # Stripe delivers the event again - nothing is lost. A broker that is
    # down does not fail the request: the event is stored, and the sweeper
    # task publishes it later.
    # Imported here: app.services.stripe_events imports this module's
    # payment helpers.
    from app.services import stripe_events

    # store_event is the one sync implementation (the Celery side uses the
    # same module); it runs on the async connection through run_sync, and
    # is looked up on the module so a test can replace it.
    stored = await db.run_sync(
        stripe_events.store_event,
        event.id,
        event.type,
        json.dumps(
            {"id": event.id, "type": event.type, "created": event.created, "data": event.data}
        ),
    )
    if not stored:
        return StripeWebhookAck(status="already_processed")
    # The one line that ties a delivery to its event: the JSON log formatter
    # adds this request's request_id (app/core/logging_config.py).
    logger.info("Stripe event %s (%s) stored", event.id, event.type)
    # Publishing to the broker is blocking network IO (apply_async). Not
    # published: marked, so the sweeper's next run publishes it.
    if not await run_in_threadpool(stripe_events.enqueue_event, event.id):
        await db.run_sync(stripe_events.mark_not_enqueued, event.id)
    return StripeWebhookAck(status="received")
