"""
Live-Stripe integration suite for the Checkout Session (hosted-redirect)
flow: exercises LiveStripePaymentProvider.create_checkout_session/
retrieve_checkout_session/expire_checkout_session against the REAL Stripe
test-mode API (api.stripe.com) - sibling to test_payment_live_stripe.py,
which covers the PaymentIntent-based /pay flow instead. Same
skip-if-no-real-key pattern, same "run deliberately, not on every commit"
rationale - see that file's module docstring for the full explanation
(not repeated here).

What this suite proves against the real API, and what it explicitly does
NOT (and per Stripe's own API surface, cannot) prove:

  - PROVEN: session creation with a real hosted `url`, retrieval,
    idempotency-key dedup, line items matching what was requested, and
    forced expiration (the only lifecycle-ending action Checkout Session's
    API actually exposes - there is no separate "cancel" endpoint, only
    `expire`; confirmed via Stripe's API reference - see
    https://docs.stripe.com/api/checkout/sessions).

  - NOT PROVEN / NOT POSSIBLE WITHOUT A BROWSER: actually completing a real
    Checkout Session (i.e. driving its hosted page to a paid state) has no
    programmatic equivalent. Stripe's own automated-testing tooling (the
    `stripe trigger checkout.session.completed` CLI helper - see
    https://docs.stripe.com/stripe-cli/use-cli) does not complete an
    existing Session at all; it fabricates an entirely separate, synthetic
    fixture event/session pair for webhook-handler testing, which is
    exactly what test_checkout_session.py's mock-suite webhook tests
    already do more directly (hand-built webhook payloads, no CLI
    dependency). There is no API call that takes a real, open Checkout
    Session id and a card number and marks it paid - the hosted page's
    card entry, 3DS challenge, and submit action are the product's entire
    reason to exist, and Stripe does not expose a bypass for them. This
    backend has no frontend to drive that page with a real browser either,
    so `checkout.session.completed`'s actual webhook-reconciliation code
    path is proven only against the mock provider (test_checkout_session.py),
    never against a truly-completed real session here.
"""
import os
import uuid
from pathlib import Path

import pytest
import stripe

from app.services.payment.exceptions import CardError
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus

# Load .env.test (if present) without clobbering anything already exported
# in the environment - see test_payment_live_stripe.py's module docstring.
_dotenv_test = Path(__file__).resolve().parents[2] / ".env.test"
if _dotenv_test.exists():
    from dotenv import load_dotenv

    load_dotenv(_dotenv_test, override=False)

_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
_HAS_REAL_KEY = _SECRET_KEY.startswith("sk_test_") and _SECRET_KEY != "sk_test_mock_placeholder"

pytestmark = pytest.mark.skipif(
    not _HAS_REAL_KEY,
    reason=(
        "No real Stripe test-mode STRIPE_SECRET_KEY in the environment "
        "(set STRIPE_SECRET_KEY or provide .env.test) - skipping the live "
        "Checkout Session integration suite."
    ),
)

SUCCESS_URL = "https://example.com/checkout/success?session_id={CHECKOUT_SESSION_ID}"
CANCEL_URL = "https://example.com/checkout/cancel"


@pytest.fixture
def provider():
    return LiveStripePaymentProvider(api_key=_SECRET_KEY)


def _idem_key() -> str:
    return str(uuid.uuid4())


class TestCreateCheckoutSession:
    def test_creates_a_real_open_session_with_hosted_url(self, provider):
        session = provider.create_checkout_session(
            amount=1099,
            currency="usd",
            idempotency_key=_idem_key(),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
            description="Test order",
        )
        assert session.id.startswith("cs_test_")
        assert session.url is not None and session.url.startswith(
            "https://checkout.stripe.com/"
        )
        assert session.status == CheckoutSessionStatus.OPEN
        # A fresh Session has no PaymentIntent yet - Stripe only attaches
        # one once the customer actually completes the hosted page (see
        # CheckoutSession's docstring in app/services/payment/types.py,
        # confirmed against a real freshly-created Session's JSON via
        # Stripe's own docs example).
        assert session.payment_intent is None
        assert session.payment_status == "unpaid"
        assert session.amount_total == 1099
        assert session.currency == "usd"

    def test_missing_success_or_cancel_url_is_rejected_by_stripe(self, provider):
        # success_url/cancel_url are mandatory params for
        # stripe.checkout.Session.create in `payment` mode - Stripe itself
        # rejects their absence, not just our own pre-validation.
        with pytest.raises(Exception):
            provider.create_checkout_session(
                amount=500,
                currency="usd",
                idempotency_key=_idem_key(),
                success_url="",
                cancel_url=CANCEL_URL,
            )


class TestIdempotency:
    def test_same_idempotency_key_does_not_create_two_sessions(self, provider):
        key = _idem_key()
        first = provider.create_checkout_session(
            amount=1500,
            currency="usd",
            idempotency_key=key,
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        second = provider.create_checkout_session(
            amount=1500,
            currency="usd",
            idempotency_key=key,
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        assert first.id == second.id


class TestRetrieve:
    def test_retrieve_returns_the_same_session(self, provider):
        created = provider.create_checkout_session(
            amount=750,
            currency="usd",
            idempotency_key=_idem_key(),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        retrieved = provider.retrieve_checkout_session(created.id)
        assert retrieved.id == created.id
        assert retrieved.status == CheckoutSessionStatus.OPEN
        assert retrieved.amount_total == 750

    def test_retrieve_nonexistent_session_raises_card_error_not_raw_stripe_error(self, provider):
        # Must surface through our abstraction's CardError(code="resource_missing"),
        # never a raw stripe.error.InvalidRequestError leaking past the
        # provider boundary - matches retrieve_payment_intent's own contract.
        with pytest.raises(CardError) as exc_info:
            provider.retrieve_checkout_session("cs_test_does_not_exist_" + uuid.uuid4().hex[:16])
        assert exc_info.value.code == "resource_missing"


class TestLineItems:
    def test_line_items_reflect_the_requested_amount_and_description(self, provider):
        # list_line_items isn't part of the PaymentProvider abstraction
        # (not needed by any orders.py call site), so this test calls the
        # real stripe SDK directly, the same way this suite would if it
        # needed to inspect any other Stripe-side detail the abstraction
        # doesn't surface.
        session = provider.create_checkout_session(
            amount=2199,
            currency="usd",
            idempotency_key=_idem_key(),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
            description="Widget order #123",
        )
        line_items = stripe.checkout.Session.list_line_items(session.id, api_key=_SECRET_KEY)
        assert len(line_items.data) == 1
        item = line_items.data[0]
        assert item.amount_total == 2199
        assert item.description == "Widget order #123"
        assert item.quantity == 1


class TestExpire:
    def test_expiring_an_open_session_marks_it_expired(self, provider):
        session = provider.create_checkout_session(
            amount=600,
            currency="usd",
            idempotency_key=_idem_key(),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        expired = provider.expire_checkout_session(session.id)
        assert expired.status == CheckoutSessionStatus.EXPIRED

        # The expiry is real and persisted server-side, not just the
        # returned object's local state.
        retrieved = provider.retrieve_checkout_session(session.id)
        assert retrieved.status == CheckoutSessionStatus.EXPIRED

    def test_expiring_an_already_expired_session_raises_card_error(self, provider):
        session = provider.create_checkout_session(
            amount=600,
            currency="usd",
            idempotency_key=_idem_key(),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        provider.expire_checkout_session(session.id)
        with pytest.raises(CardError):
            provider.expire_checkout_session(session.id)
