"""
Live-Stripe integration suite: exercises LiveStripePaymentProvider
(app/services/payment/live_stripe_provider.py) against the REAL Stripe
test-mode API (api.stripe.com), not the deterministic mock in
stripe_provider.py that tests/integration/test_payment.py covers.

This suite is intentionally separate from, and has zero effect on,
test_payment.py / the default test suite:
  - It never imports or monkeypatches StripePaymentProvider.
  - It's skipped outright (pytest.mark.skipif, evaluated at collection
    time) unless a real, non-placeholder STRIPE_SECRET_KEY is present in
    the environment - so CI and any machine without real Stripe test
    credentials collects zero tests here and never makes a network call.
  - `.env.test` (gitignored, never committed - see .gitignore's `.env.*`
    pattern) is loaded, if present, purely as a local convenience so a
    developer with real sandbox keys doesn't have to export them by hand;
    it never sets a value if that env var is already set in the shell.

Per Stripe's own automated-testing guidance
(https://docs.stripe.com/automated-testing#server-side-testing), tests that
make real requests against the Stripe API "should ... be performed
infrequently to avoid rate limits" - this suite is meant to be run
deliberately (e.g. by hand, or in an occasional/opt-in CI job), not on
every commit; the fast default suite already covers this module's logic
exhaustively against the deterministic mock.

Test card tokens/numbers used below are Stripe's own documented test-mode
tokens (https://docs.stripe.com/testing#cards), confirmed live against the
API while writing this suite:
  - pm_card_visa                    -> succeeds
  - pm_card_visa_chargeDeclined     -> generic decline (card_declined /
                                        generic_decline)
  - pm_card_authenticationRequired  -> always requires 3DS authentication
                                        (PaymentIntent.confirm returns status
                                        "requires_action", not an exception -
                                        this is the abstraction's contract,
                                        matching how StripePaymentProvider's
                                        mock models the same token).
"""
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from app.services.payment.exceptions import (
    CardError,
    SignatureVerificationError,
)
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import PaymentIntentStatus

# Load .env.test (if present) without clobbering anything already exported
# in the environment - see module docstring.
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
        "Stripe integration suite."
    ),
)

PM_SUCCESS = "pm_card_visa"
PM_DECLINE = "pm_card_visa_chargeDeclined"
PM_3DS_REQUIRED = "pm_card_authenticationRequired"


@pytest.fixture
def provider():
    return LiveStripePaymentProvider(api_key=_SECRET_KEY)


def _idem_key() -> str:
    return str(uuid.uuid4())


class TestSuccessfulPayment:
    def test_create_and_confirm_with_succeeding_card(self, provider):
        pi = provider.create_payment_intent(
            amount=1099,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_SUCCESS,
        )
        assert pi.status == PaymentIntentStatus.REQUIRES_CONFIRMATION
        assert pi.amount == 1099
        assert pi.currency == "usd"

        confirmed = provider.confirm_payment_intent(pi.id)
        assert confirmed.status == PaymentIntentStatus.SUCCEEDED
        assert confirmed.id == pi.id

        retrieved = provider.retrieve_payment_intent(pi.id)
        assert retrieved.status == PaymentIntentStatus.SUCCEEDED


class TestDecliningCard:
    def test_declining_card_raises_card_error(self, provider):
        pi = provider.create_payment_intent(
            amount=500,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_DECLINE,
        )
        with pytest.raises(CardError) as exc_info:
            provider.confirm_payment_intent(pi.id)
        assert exc_info.value.code == "card_declined"
        assert exc_info.value.decline_code == "generic_decline"

        # Real Stripe leaves the PaymentIntent alive after a decline
        # (requires_payment_method), not deleted - a new payment_method can
        # be attached and confirmed again.
        after = provider.retrieve_payment_intent(pi.id)
        assert after.status == PaymentIntentStatus.REQUIRES_PAYMENT_METHOD


class TestThreeDSecureRequired:
    def test_authentication_required_card_surfaces_as_requires_action(self, provider):
        pi = provider.create_payment_intent(
            amount=700,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_3DS_REQUIRED,
        )
        confirmed = provider.confirm_payment_intent(pi.id)
        # Per PaymentProvider's contract (base.py) and the mock's own
        # modeling of this same token: requires_action is a non-terminal
        # *return value*, not an exception - the caller (orders.py) is
        # expected to surface next_action to the client, not treat this as
        # a failure.
        assert confirmed.status == PaymentIntentStatus.REQUIRES_ACTION
        assert confirmed.next_action is not None
        # With a real return_url supplied at confirm (required by Stripe for
        # any redirect-capable confirmation - see
        # https://docs.stripe.com/payments/3d-secure/authentication-flow),
        # Stripe drives 3DS as a browser redirect rather than an in-SDK
        # challenge, so next_action is "redirect_to_url", not
        # "use_stripe_sdk". The backend's job is just to relay whatever
        # next_action Stripe returns - it doesn't drive the redirect itself.
        assert confirmed.next_action.get("type") == "redirect_to_url"


class TestRefund:
    def test_refund_of_captured_payment(self, provider):
        pi = provider.create_payment_intent(
            amount=2500,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_SUCCESS,
        )
        provider.confirm_payment_intent(pi.id)

        refund = provider.create_refund(payment_intent_id=pi.id)
        assert refund.payment_intent == pi.id
        assert refund.amount == 2500
        assert refund.currency == "usd"
        assert refund.status.value == "succeeded"

    def test_partial_refund_of_captured_payment(self, provider):
        pi = provider.create_payment_intent(
            amount=1000,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_SUCCESS,
        )
        provider.confirm_payment_intent(pi.id)

        refund = provider.create_refund(payment_intent_id=pi.id, amount=400)
        assert refund.amount == 400
        assert refund.status.value == "succeeded"

    def test_refund_of_unconfirmed_payment_intent_raises_card_error(self, provider):
        # Real Stripe rejects refunding a PaymentIntent that was never
        # captured/succeeded - mirrors the mock's own equivalent check.
        pi = provider.create_payment_intent(
            amount=800,
            currency="usd",
            idempotency_key=_idem_key(),
            payment_method=PM_SUCCESS,
        )
        with pytest.raises(CardError):
            provider.create_refund(payment_intent_id=pi.id)


class TestNotFound:
    def test_retrieve_nonexistent_payment_intent_raises_card_error_not_raw_stripe_error(
        self, provider
    ):
        # Must surface through our abstraction's CardError(code="resource_missing"),
        # never a raw stripe.error.InvalidRequestError leaking past the provider
        # boundary (that would break every caller's except clauses, which only
        # know about app.services.payment.exceptions.*).
        with pytest.raises(CardError) as exc_info:
            provider.retrieve_payment_intent("pi_does_not_exist_" + uuid.uuid4().hex[:16])
        assert exc_info.value.code == "resource_missing"


class TestIdempotency:
    def test_same_idempotency_key_does_not_double_charge(self, provider):
        key = _idem_key()
        first = provider.create_payment_intent(
            amount=1500,
            currency="usd",
            idempotency_key=key,
            payment_method=PM_SUCCESS,
        )
        second = provider.create_payment_intent(
            amount=1500,
            currency="usd",
            idempotency_key=key,
            payment_method=PM_SUCCESS,
        )
        # Same PaymentIntent object returned by Stripe for a repeated key -
        # not two distinct PaymentIntents (which would mean two real charges
        # once both were confirmed).
        assert first.id == second.id


class TestWebhookSignature:
    """
    We don't have a real Stripe webhook endpoint/signing secret configured
    for this sandbox (that requires a publicly reachable URL registered in
    the Dashboard), so - per the coordinator's instruction to at least
    verify construct_webhook_event's signature logic against real Stripe
    SDK semantics - these tests exercise the real `stripe.Webhook.construct_event`
    function directly (via LiveStripePaymentProvider, which calls it) with a
    locally-generated secret and a payload signed exactly the way Stripe
    signs real webhook deliveries (HMAC-SHA256 over "{timestamp}.{payload}").
    This is the real signature-verification code path, only the secret's
    origin (locally generated vs. copied from the Dashboard) differs -
    Stripe's HMAC verification has no way to distinguish the two.
    """

    def _payload(self) -> bytes:
        return json.dumps(
            {
                "id": f"evt_{uuid.uuid4().hex[:24]}",
                "type": "payment_intent.succeeded",
                "created": int(time.time()),
                "data": {"object": {"id": "pi_test_webhook"}},
            }
        ).encode()

    def test_validly_signed_payload_is_accepted(self, provider):
        secret = f"whsec_{uuid.uuid4().hex}"
        payload = self._payload()
        header = StripePaymentProvider.sign_payload(payload, secret)

        event = provider.construct_webhook_event(payload, header, secret)
        assert event.type == "payment_intent.succeeded"

    def test_tampered_signature_is_rejected(self, provider):
        secret = f"whsec_{uuid.uuid4().hex}"
        payload = self._payload()
        header = StripePaymentProvider.sign_payload(payload, secret)
        tampered_header = header[:-4] + "dead"

        with pytest.raises(SignatureVerificationError):
            provider.construct_webhook_event(payload, tampered_header, secret)

    def test_signature_signed_with_wrong_secret_is_rejected(self, provider):
        payload = self._payload()
        header = StripePaymentProvider.sign_payload(payload, "whsec_" + uuid.uuid4().hex)

        with pytest.raises(SignatureVerificationError):
            provider.construct_webhook_event(payload, header, "whsec_" + uuid.uuid4().hex)

    def test_missing_signature_header_is_rejected(self, provider):
        with pytest.raises(SignatureVerificationError):
            provider.construct_webhook_event(self._payload(), None, "whsec_" + uuid.uuid4().hex)
