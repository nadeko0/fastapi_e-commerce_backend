"""close_attempts_at_provider: what each attempt needs, and what a failure means.

Run against the in-process provider (app/services/payment/stripe_provider.py),
which refuses to expire or cancel what is no longer open, as Stripe does."""

import uuid

import pytest

from app.services.orders import close_attempts_at_provider
from app.services.payment.exceptions import CardError, PaymentProviderTimeoutError
from app.services.payment.stripe_provider import TOKEN_SUCCESS_DEFAULT, StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus, PaymentIntentStatus


@pytest.fixture
def provider():
    StripePaymentProvider.reset_mock_state()
    yield StripePaymentProvider()
    StripePaymentProvider.reset_mock_state()


def _session(provider):
    return provider.create_checkout_session(
        amount=500, currency="USD", idempotency_key=uuid.uuid4().hex,
        success_url="https://example.com/s", cancel_url="https://example.com/c",
    )


def _intent(provider):
    return provider.create_payment_intent(
        amount=500, currency="USD", idempotency_key=uuid.uuid4().hex,
    )


def test_a_session_is_expired_and_an_intent_cancelled(provider):
    session, intent = _session(provider), _intent(provider)

    assert close_attempts_at_provider(
        provider, [(session.id, "pending-cs-1"), (None, intent.id)]) is True
    assert provider.retrieve_checkout_session(session.id).status == CheckoutSessionStatus.EXPIRED
    assert provider.retrieve_payment_intent(intent.id).status == PaymentIntentStatus.CANCELED


def test_an_attempt_that_never_reached_the_provider_needs_nothing(provider):
    assert close_attempts_at_provider(provider, [(None, "pending-abc")]) is True


def test_an_attempt_already_closed_at_the_provider_counts_as_closed(provider):
    session, intent = _session(provider), _intent(provider)
    provider.expire_checkout_session(session.id)
    provider.cancel_payment_intent(intent.id)

    assert close_attempts_at_provider(
        provider, [(session.id, "pending-cs-1"), (None, intent.id)]) is True


def test_an_attempt_unknown_to_the_provider_counts_as_closed(provider):
    assert close_attempts_at_provider(
        provider, [("cs_test_missing", "pending-cs-1"), (None, "pi_missing")]) is True


def test_a_paid_attempt_is_not_closed(provider):
    # The expiry sweep used to take the provider's refusal for "closed" and
    # cancel and restock an order whose customer had paid, its webhook late.
    session = _session(provider)
    session.status, session.payment_status = CheckoutSessionStatus.COMPLETE, "paid"
    intent = provider.create_payment_intent(
        amount=500, currency="USD", idempotency_key=uuid.uuid4().hex,
        payment_method=TOKEN_SUCCESS_DEFAULT,
    )
    provider.confirm_payment_intent(intent.id, return_url="https://example.com/r")
    assert provider.retrieve_payment_intent(intent.id).status == PaymentIntentStatus.SUCCEEDED

    assert close_attempts_at_provider(provider, [(session.id, "pending-cs-1")]) is False
    assert close_attempts_at_provider(provider, [(None, intent.id)]) is False


def test_a_refused_attempt_that_cannot_be_read_is_not_closed(provider, monkeypatch):
    session = _session(provider)
    provider.expire_checkout_session(session.id)

    def unreachable(self, session_id):
        raise PaymentProviderTimeoutError("timeout")

    monkeypatch.setattr(StripePaymentProvider, "retrieve_checkout_session", unreachable)
    assert close_attempts_at_provider(provider, [(session.id, "pending-cs-1")]) is False


def test_an_unreachable_provider_is_reported_and_the_rest_are_still_tried(provider, monkeypatch):
    session, intent = _session(provider), _intent(provider)

    def unreachable(self, intent_id):
        raise PaymentProviderTimeoutError("timeout")

    monkeypatch.setattr(StripePaymentProvider, "cancel_payment_intent", unreachable)
    assert close_attempts_at_provider(
        provider, [(None, intent.id), (session.id, "pending-cs-1")]) is False
    assert provider.retrieve_checkout_session(session.id).status == CheckoutSessionStatus.EXPIRED


def test_a_refusal_without_a_code_is_still_checked_at_the_provider(provider, monkeypatch):
    intent = _intent(provider)

    def refused(self, intent_id):
        raise CardError("refused", code="payment_intent_unexpected_state")

    monkeypatch.setattr(StripePaymentProvider, "cancel_payment_intent", refused)
    assert close_attempts_at_provider(provider, [(None, intent.id)]) is False
