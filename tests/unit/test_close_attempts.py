"""close_attempts_at_provider: what each attempt needs, and what a failure means."""

from app.services.orders import close_attempts_at_provider
from app.services.payment.exceptions import (
    CardError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
)


class _Provider:
    def __init__(self, error=None):
        self.error = error
        self.expired = []
        self.cancelled = []

    def expire_checkout_session(self, session_id):
        self.expired.append(session_id)
        if self.error:
            raise self.error

    def cancel_payment_intent(self, intent_id):
        self.cancelled.append(intent_id)
        if self.error:
            raise self.error


def test_a_session_is_expired_and_an_intent_cancelled():
    provider = _Provider()
    assert close_attempts_at_provider(provider, [("cs_1", "pending-cs-1"), (None, "pi_1")]) is True
    assert provider.expired == ["cs_1"]
    assert provider.cancelled == ["pi_1"]


def test_an_attempt_that_never_reached_the_provider_needs_nothing():
    provider = _Provider()
    assert close_attempts_at_provider(provider, [(None, "pending-abc")]) is True
    assert provider.cancelled == []


def test_a_refusal_counts_as_closed():
    for error in (CardError("paid already", code="x"), InvalidRequestError("not open")):
        assert close_attempts_at_provider(_Provider(error), [(None, "pi_1")]) is True


def test_an_unreachable_provider_is_reported_and_the_rest_are_still_tried():
    provider = _Provider(PaymentProviderTimeoutError("timeout"))
    assert close_attempts_at_provider(provider, [(None, "pi_1"), ("cs_1", "pending-cs-1")]) is False
    assert provider.cancelled == ["pi_1"]
    assert provider.expired == ["cs_1"]
